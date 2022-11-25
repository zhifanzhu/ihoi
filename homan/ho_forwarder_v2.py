from hydra.utils import to_absolute_path
from typing import Tuple, List
from functools import reduce
import torch
import torch.nn as nn
import torch.nn.functional as F
import neural_renderer as nr
from pytorch3d.ops import knn_points, knn_gather
from torch_scatter import scatter_min

from nnutils.handmocap import get_hand_faces
from nnutils.mesh_utils_extra import compute_vert_normals
from homan.contact_prior import get_contact_regions
from homan.homan_ManoModel import HomanManoModel
from homan.utils.geometry import matrix_to_rot6d, rot6d_to_matrix
from homan.ho_utils import (
    compute_transformation_ortho, compute_transformation_persp)

from homan.lossutils import (
    compute_ordinal_depth_loss, compute_contact_loss,
    compute_collision_loss,
    iou_loss, rotation_loss_v1, compute_chamfer_distance,
    compute_nearest_dist, find_nearest_vecs)
import roma

from libzhifan.numeric import check_shape
from libzhifan.geometry import BatchCameraManager
from libyana.metrics.iou import batch_mask_iou


class HOForwarderV2(nn.Module):

    def __init__(self,
                 camintr: torch.Tensor):
        """
        Args:
            camintr: (B, 3, 3).
                Ihoi bounding box camera.
        """
        super().__init__()
        bsize = len(camintr)
        self.bsize = bsize
        self.mask_size = 256
        self.register_buffer("camintr", camintr)
        self.contact_regions = get_contact_regions()

        """ Set-up silhouettes renderer """
        self.renderer = nr.renderer.Renderer(
            image_size=self.mask_size,
            K=camintr.clone().cuda(),
            R=torch.eye(3, device='cuda')[None],
            t=torch.zeros([1, 3], device='cuda'),
            orig_size=1)
        self.renderer.light_direction = [1, 0.5, 1]
        self.renderer.light_intensity_direction = torch.as_tensor(0.3)
        self.renderer.light_intensity_ambient = 0.5
        self.renderer.background_color = [1.0, 1.0, 1.0]

    """ Common functions """

    def checkpoint(self, session=0):
        torch.save(self.state_dict(), f'/tmp/h{session}.pth')

    def resume(self, session=0):
        self.load_state_dict(torch.load(f'/tmp/h{session}.pth'), strict=True)

    """ Hand functions """

    def set_hand_params(self,
                        rotations_hand,
                        translations_hand,
                        hand_side: str,
                        mano_pca_pose,
                        mano_betas,
                        mano_trans=None,
                        mano_rot=None,
                        scale_hand=1.0):
        """ Inititalize person parameters """
        self.hand_sides = [hand_side]
        self.hand_nb = 1
        if mano_trans is None:
            mano_trans = torch.zeros([self.bsize, 3], device=mano_pca_pose.device)
        if mano_rot is None:
            mano_rot = torch.zeros([self.bsize, 3], device=mano_pca_pose.device)

        self.mano_model = HomanManoModel(
            to_absolute_path("externals/mano"), side=hand_side, pca_comps=45)  # Note(zhifan): in HOMAN num_pca = 16
        translation_init = translations_hand.detach().clone()
        self.translations_hand = nn.Parameter(translation_init,
                                              requires_grad=True)
        rotations_hand = rotations_hand.detach().clone()
        if rotations_hand.shape[-1] == 3:
            rotations_hand = matrix_to_rot6d(rotations_hand)
        self.rotations_hand = nn.Parameter(rotations_hand, requires_grad=True)

        self.mano_pca_pose = nn.Parameter(mano_pca_pose, requires_grad=True)
        self.mano_rot = nn.Parameter(mano_rot, requires_grad=True)
        self.mano_trans = nn.Parameter(mano_trans, requires_grad=True)
        self.mano_betas = nn.Parameter(torch.zeros_like(mano_betas),
                                        requires_grad=True)
        self.scale_hand = nn.Parameter(
            scale_hand * torch.ones(1).float(),
            requires_grad=True)

        faces_hand = get_hand_faces(hand_side)
        num_faces_hand = faces_hand.size(1)
        self.register_buffer(
            "textures_hand",
            torch.ones(self.bsize, num_faces_hand, 1, 1, 1, 3))
        self.register_buffer(
            "faces_hand", faces_hand.expand(self.bsize, num_faces_hand, 3))
        self.cuda()

    def set_hand_target(self, target_masks_hand):
        self.register_buffer("ref_mask_hand", (target_masks_hand > 0).float())
        self.register_buffer("keep_mask_hand",
                             (target_masks_hand >= 0).float())
        mask_h, mask_w = target_masks_hand.shape[-2:]
        self.register_buffer(
            "masks_human",
            target_masks_hand.view(self.bsize, 1, mask_h, mask_w).bool())
        self.cuda()
        self._check_shape_hand(self.bsize)

    def _check_shape_hand(self, bsize):
        check_shape(self.faces_hand, (bsize, -1, 3))
        check_shape(self.camintr, (bsize, 3, 3))
        check_shape(self.ref_mask_hand, (bsize, self.mask_size, self.mask_size))
        mask_shape = self.ref_mask_hand.shape
        check_shape(self.keep_mask_hand, mask_shape)
        check_shape(self.rotations_hand, (bsize, 3, 2))
        check_shape(self.translations_hand, (bsize, 1, 3))
        # ordinal loss
        check_shape(self.masks_human, (bsize, 1, self.mask_size, self.mask_size))

    @property
    def rot_mat_hand(self) -> torch.Tensor:
        return rot6d_to_matrix(self.rotations_hand)

    def get_verts_hand(self, detach_scale=False, hand_space=False) -> torch.Tensor:
        """
        Args:
            hand_space: if True, return hand vertices in hand space itself.
        """
        all_hand_verts = []
        for hand_idx, side in enumerate(self.hand_sides):
            mano_pca_pose = self.mano_pca_pose[hand_idx::self.hand_nb]
            mano_rot = self.mano_rot[hand_idx::self.hand_nb]
            mano_res = self.mano_model.forward_pca(
                mano_pca_pose,
                rot=mano_rot,
                betas=self.mano_betas[hand_idx::self.hand_nb],
                side=side)
            vertices = mano_res["verts"]
            all_hand_verts.append(vertices)
        all_hand_verts = torch.stack(all_hand_verts).transpose(
            0, 1).contiguous().view(-1, 778, 3)
        verts_hand_og = all_hand_verts + self.mano_trans.unsqueeze(1)
        if hand_space:
            return verts_hand_og

        scale = self.scale_hand.detach() if detach_scale else self.scale_hand
        rotations_hand = self.rot_mat_hand

        hand_proj_mode = 'persp'
        if hand_proj_mode == "ortho":
            raise NotImplementedError
            return compute_transformation_ortho(
                meshes=verts_hand_og,
                cams=self.cams_hand,
                intrinsic_scales=scale,
                K=self.renderer.K,
                img=self.masks_human,
            )
        elif hand_proj_mode == "persp":
            return compute_transformation_persp(
                meshes=verts_hand_og,
                translations=self.translations_hand,
                rotations=rotations_hand,
                intrinsic_scales=scale,
            )
        else:
            raise ValueError(
                f"Expected hand_proj_mode {self.hand_proj_mode} to be in [ortho|persp]"
            )

    """ Object functions """

    def set_obj_transform(self,
                          translations_object,
                          rotations_object,
                          scale_object):
        """ Initialize / Re-set object parameters

        Args:
            obj_trans: (N, 3, 3)
        """
        if rotations_object.shape[-1] == 3:
                rotations_object6d = matrix_to_rot6d(rotations_object)
        else:
            rotations_object6d = rotations_object
        self.rotations_object = nn.Parameter(
            rotations_object6d.detach().clone(), requires_grad=True)
        self.translations_object = nn.Parameter(
            translations_object.view(-1, 1, 3).detach().clone(),
            requires_grad=True)
        """ Translation is also a function of scale T(s) = s * T_init """
        self.scale_object = nn.Parameter(
            scale_object.detach().clone(),
            requires_grad=True)

    def set_obj_params(self,
                       translations_object,
                       rotations_object,
                       verts_object_og,
                       faces_object,
                       scale_mode,
                       scale_init):
        """ Initialize object pamaters

        Args:
            obj_trans: (N, 3, 3)
            scale_mode: str, one of {'depth', 'scalar', 'xyz'}
                but use scale_init if provided.
            scale_init: (N,) for scale_mode != 'xyz'
        """
        self.num_obj = len(rotations_object)
        self.scale_mode = scale_mode
        self.set_obj_transform(
            translations_object, rotations_object, scale_init)
        self.register_buffer("verts_object_og", verts_object_og)
        """ Do not attempt to copy tensor too early, which will get OOM. """
        self.register_buffer(
            "faces_object", faces_object)
        self.register_buffer(
            "textures_object",
            torch.ones(faces_object.shape[0], 1, 1, 1, 3))

    def set_obj_target(self, target_masks_object: torch.Tensor):
        """
        Args:
            target_masks_object: (B, W, W)
        """
        target_masks_object = target_masks_object
        self.register_buffer("ref_mask_object",
                             (target_masks_object > 0).float())
        self.register_buffer("keep_mask_object",
                             (target_masks_object >= 0).float())
        # edge loss is not used
        self.cuda()
        self._check_shape_object(self.num_obj)

    def _check_shape_object(self, num_init):
        check_shape(self.verts_object_og, (-1, 3))
        check_shape(self.faces_object, (-1, 3))
        check_shape(self.ref_mask_object,  (self.bsize, self.mask_size, self.mask_size))
        mask_shape = self.ref_mask_object.shape
        check_shape(self.keep_mask_object, mask_shape)
        check_shape(self.rotations_object, (num_init, 3, 2))
        check_shape(self.translations_object, (num_init, 1, 3))
        if self.scale_mode == 'xyz':
            check_shape(self.scale_object, (num_init, 3))
        else:
            check_shape(self.scale_object, (num_init,))

    def _expand_obj_faces(self, *precedings) -> torch.Tensor:
        """
        Args:
            *precedings:
                e.g. self._expand_obj_faces(b, n) -> shape (b, n, f, 3)

        Returns:
            obj_faces: (*precedings, F_o, 3)
        """
        num_obj_faces = self.faces_object.size(0)
        return self.faces_object.expand(*precedings, num_obj_faces, 3)

    @property
    def rot_mat_obj(self) -> torch.Tensor:
        return rot6d_to_matrix(self.rotations_object)

    def get_verts_object(self, cam_idx=None, transl_gradient_only=False) -> torch.Tensor:
        """
            V_out = (V_model x R_o2h + T_o2h) x R_hand + T_hand
                  = V x (R_o2h x R_hand) + (T_o2h x R_hand + T_hand)
        where self.rotations/translations_object is R/T_o2h from object to hand

        Args:
            cam_idx: int

        Returns:
            verts_object: (B, N, V, 3)
                or (1, N, V, 3) if cam_idx is not one
        """
        R_o2h = self.rot_mat_obj  # (N, 3, 3)
        T_o2h = self.translations_object  # (N, 1, 3)
        scale = self.scale_object
        """ Compound T_o2c (T_obj w.r.t camera) = T_h2c x To2h_ """
        R_hand = self.rot_mat_hand  # (B, 3, 3)
        T_hand = self.translations_hand  # (B, 1, 3)
        if cam_idx is not None:
            R_hand = R_hand[[cam_idx]]
            T_hand = T_hand[[cam_idx]]
        rots = R_o2h.unsqueeze(0) @ R_hand.unsqueeze(1)
        transl = torch.add(
                torch.matmul(
                    T_o2h.unsqueeze(0),  # (N, 1, 3) -> (1, N, 1, 3)
                    R_hand.unsqueeze(1),  # (B, 3, 3) -> (B, 1, 3, 3)
                ),  # (B, N, 1, 3)
                T_hand.unsqueeze(1),  # (B, 1, 3) -> (B, 1, 1, 3)
            ) # (B, N, 1, 3)
        if self.scale_mode == 'depth':
            transl = scale * transl
        elif self.scale_mode == 'scalar':
            transl = transl
        elif self.scale_mode == 'xyz':
            transl = transl
        else:
            raise ValueError("scale_mode not understood")

        verts_obj = self.verts_object_og.view(1, 1, -1, 3).expand(
            self.bsize, self.num_obj, -1, -1)
        if self.scale_mode == 'xyz':
            scale = scale.view(1, -1, 1, 3).expand(
                self.bsize, -1, -1, -1)
        else:
            scale = scale.view(1, -1, 1, 1).expand(
                self.bsize, -1, -1, -1)
        return torch.matmul(verts_obj * scale, rots) + transl


class HOForwarderV2Impl(HOForwarderV2):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

    def loss_sil_hand(self, compute_iou=False, func='iou'):
        """ returns: (B,) """
        rend = self.renderer(
            self.get_verts_hand(),
            self.faces_hand,
            K=self.camintr,
            mode="silhouettes")
        image = self.keep_mask_hand * rend
        if func == 'l2':
            loss_sil = torch.sum(
                (image - self.ref_mask_hand)**2, dim=(1, 2))
            loss_sil = loss_sil / self.keep_mask_hand.sum(dim=(1,2))
        elif func == 'iou':
            loss_sil = iou_loss(image, self.ref_mask_hand)
        elif func == 'l2_iou':
            loss_sil = torch.sum(
                (image - self.ref_mask_hand)**2, dim=(1, 2))
            loss_sil = loss_sil / self.keep_mask_hand.sum(dim=(1,2))
            with torch.no_grad():
                iou_factor = iou_loss(image, self.ref_mask_hand, post='rev')
            loss_sil = loss_sil * iou_factor

        # loss_sil = loss_sil / self.bsize
        if compute_iou:
            ious = batch_mask_iou(image, self.ref_mask_hand)
            return loss_sil, ious
        return loss_sil

    def loss_pca_interpolation(self) -> torch.Tensor:
        """
        Interpolation Prior: pose(t) = (pose(t+1) + pose(t-1)) / 2

        Returns: (B-2,)
        """
        target = (self.mano_pca_pose[2:] + self.mano_pca_pose[:-2]) / 2
        pred = self.mano_pca_pose[1:-1]
        loss = torch.sum((target - pred)**2, dim=(1))
        return loss

    def loss_hand_rot(self) -> torch.Tensor:
        """ Interpolation loss for hand rotation """
        device = self.rotations_hand.device
        rotmat = self.rot_mat_hand
        rot_mid = roma.rotmat_slerp(
            rotmat[2:], rotmat[:-2],
            torch.as_tensor([0.5], device=device))[0]
        loss = rotation_loss_v1(rot_mid, rotmat[1:-1])
        return loss

    def loss_hand_transl(self) -> torch.Tensor:
        """
        Returns: (B-2,)
        """
        interp = (self.translations_hand[2:] + self.translations_hand[:-2]) / 2
        pred = self.translations_hand[1:-1]
        loss = torch.sum((interp - pred)**2, dim=(1, 2))
        return loss

    def forward_hand(self,
                     loss_weights={
                         'sil': 1,
                         'pca': 1,
                         'rot': 10,
                         'transl': 1,
                     }) -> Tuple[torch.Tensor, dict]:
        l_sil = self.loss_sil_hand(compute_iou=False, func='iou').sum()
        l_pca = self.loss_pca_interpolation().sum()
        l_rot = self.loss_hand_rot().sum()
        l_transl = self.loss_hand_transl().sum()
        losses = {
            'sil': l_sil,
            'pca': l_pca,
            'rot': l_rot,
            'transl': l_transl,
        }
        for k, l in losses.items():
            losses[k] = l * loss_weights[k]
        total_loss = sum([v for v in losses.values()])
        return total_loss, losses

    """ Object functions """

    def diff_proj_center(self):
        """ (B,) """
        gt_center_2d = torch.cat(
            [v.nonzero().float().mean(0, keepdim=True)
             for v in self.ref_mask_object],
            axis=0)  # (B, 2)
        v_obj = self.get_verts_object()
        v_homo = v_obj / v_obj[..., [-1]]
        v_proj = torch.einsum('bijk,blk->bjl', v_homo, self.camintr)
        v_2d = v_proj[..., :2]  # (B, V, 2)
        v_center_2d = v_2d.mean(1) * self.mask_size  # (B, 2)

        dist = (gt_center_2d - v_center_2d) / self.mask_size
        diff = (dist**2).abs_()
        return diff

    def render_obj(self, verts=None, sample_indices=None) -> torch.Tensor:
        """
        Renders objects according to current rotation and translation.

        Returns:
            images: ndarray (B, N_init, W, W)
        """
        if verts is None:
            verts = self.get_verts_object()  # (B, N, V, 3)
        b = verts.size(0)
        n = verts.size(1)
        batch_faces = self._expand_obj_faces(b, n)
        batch_K = self.camintr.unsqueeze(1).expand(self.bsize, n, 3, 3)  # (B, 3, 3) -> (B, N, 3, 3)
        if sample_indices is None:
            sample_indices = np.arange(self.bsize)
        batch_K = batch_K[sample_indices, ...]

        images = self.renderer(
            verts.view(b*n, -1, 3),
            batch_faces.view(b*n, -1, 3),
            K=batch_K.view(b*n, -1, 3),
            mode='silhouettes')
        images = images.view(b, n, self.mask_size, self.mask_size)
        return images

    def simple_obj_sil(self,
                       frame_idx,
                       ret_iou=False,
                       func='l2') -> dict:
        """  For find_optimal_obj_pose()
        returns: (N,)
        """
        _, n, w = self.bsize, self.num_obj, self.mask_size
        verts = self.get_verts_object(frame_idx)
        image = self.render_obj(verts, cam_idx=frame_idx)  # (1, N, W, W)
        image = image * self.keep_mask_object[[frame_idx], None]
        image_ref = self.ref_mask_object[[frame_idx], None]  # (1, 1, W, W)
        loss_dict = {}
        if func == 'l2':
            loss_mask = torch.sum((image - image_ref)**2, dim=(2, 3))
            loss_mask = loss_mask / (n * image_ref.sum(dim=(-2,-1)))
        elif func == 'iou':
            loss_mask = iou_loss(image, image_ref)
        elif func == 'l2_iou':
            raise NotImplementedError
            loss_sil = torch.sum(
                (image - self.ref_mask_object)**2, dim=(-2, -1))
            loss_sil = loss_sil / image_ref.sum(dim=(-2,-1))
            with torch.no_grad():
                iou_factor = iou_loss(image, image_ref, post='rev')
            loss_sil = loss_sil * iou_factor

        loss_dict["mask"] = loss_mask.squeeze_(0)

        if ret_iou:
            with torch.no_grad():
                iou = batch_mask_iou(
                    image.view(n, w, w).detach(),
                    image_ref.repeat(1, n, 1, 1).view(n, w, w).detach())  # (B*N,)
                iou = iou.view(n)  # (B, N)
            return iou

        return loss_dict

    def forward_obj_pose_render(self,
                                loss_only=True,
                                func='l2_iou',
                                sample_indices=None) -> dict:
        """ Reimplement the PoseRenderer.foward()

        Args:
            v_obj (torch.Tensor): (B, N, V, 3)

        Returns:
            loss_dict: dict with
                - mask: (B, N)
                - offscreen: (B, N)
        """
        b, n, w = self.bsize, self.num_obj, self.mask_size
        if sample_indices is None:
            sample_indices = np.arange(self.bsize)

        v_obj = self.get_verts_object()[sample_indices, ...]
        image = self.render_obj(v_obj, sample_indices=sample_indices)  # (B, N, W, W)
        keep = self.keep_mask_object[sample_indices, None]
        image = keep * image
        image_ref = self.ref_mask_object[sample_indices, None]  # (B, 1, W, W)

        loss_dict = {}
        if func == 'l2':
            loss_mask = torch.sum((image - image_ref)**2, dim=(-2,-1))
            loss_mask = loss_mask / (n*image_ref.sum(dim=(-2,-1)))
        elif func == 'iou':
            loss_mask = iou_loss(image, image_ref)
        elif func == 'l2_iou':
            loss_mask = torch.sum(
                (image - image_ref)**2, dim=(-2,-1))
            loss_mask = loss_mask / (n*image_ref.sum(dim=(-2,-1)))
            with torch.no_grad():
                iou_factor = iou_loss(image, image_ref, post='rev')
            loss_mask = loss_mask * iou_factor

        loss_dict["mask"] = loss_mask
        if not loss_only:
            with torch.no_grad():
                iou = batch_mask_iou(
                    image.view(b*n, w, w).detach(),
                    image_ref.repeat(1, n, 1, 1).view(b*n, w, w).detach())  # (B*N,)
                iou = iou.view(b, n)  # (B, N)
        loss_dict["offscreen"] = 100000 * self.compute_offscreen_loss(
            v_obj, sample_indices=sample_indices)
        if not loss_only:
            return loss_dict, iou, image
        else:
            return loss_dict

    def compute_offscreen_loss(self, verts, sample_indices=None):
        """
        Computes loss for offscreen penalty. This is used to prevent the degenerate
        solution of moving the object offscreen to minimize the chamfer loss.

        Args:
            verts: (B, N, V, 3)

        Returns:
            loss: (B, N)
        """
        # On-screen means coord_xy between [-1, 1] and far > depth > 0
        b, n = verts.size(0), verts.size(1)
        batch_K = self.camintr.unsqueeze(1).expand(self.bsize, n, 3, 3)  # (B, N, 3, 3)
        if sample_indices is None:
            sample_indices = np.arange(self.bsize)
        batch_K = batch_K[sample_indices, ...]
        proj = nr.projection(
            verts.view(b*n, -1, 3),
            batch_K.view(b*n, 3, 3),
            self.renderer.R,
            self.renderer.t,
            self.renderer.dist_coeffs,
            orig_size=1,
        )  # (B*N, ...)
        coord_xy, coord_z = proj[:, :, :2], proj[:, :, 2:]
        zeros = torch.zeros_like(coord_z)
        lower_right = torch.max(coord_xy - 1,
                                zeros).sum(dim=(1, 2))  # Amount greater than 1
        upper_left = torch.max(-1 - coord_xy,
                               zeros).sum(dim=(1, 2))  # Amount less than -1
        behind = torch.max(-coord_z, zeros).sum(dim=(1, 2))
        too_far = torch.max(coord_z - self.renderer.far, zeros).sum(dim=(1, 2))
        loss = lower_right + upper_left + behind + too_far  # (B*N)
        return loss.view(b, n)

    """ Hand-Object interaction """

    def loss_ordinal_depth(self, v_hand=None, v_obj=None):
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        b = v_obj.size(0)

        _, depths_o, silhouettes_o = self.renderer.render(
            v_obj[:, 0], self._expand_obj_faces(b),
            self.textures_object.expand(b, *self.textures_object.shape))  # (B, 256, 256)
        silhouettes_o = (silhouettes_o == 1).bool()

        _, depths_p, silhouettes_p = self.renderer.render(
            v_hand, self.faces_hand, self.textures_hand)
        silhouettes_p = (silhouettes_p == 1).bool()

        all_masks = torch.stack(
            [self.ref_mask_object.bool(), self.ref_mask_hand.bool()],
            axis=1)
        silhouettes = [silhouettes_o, silhouettes_p]
        depths = [depths_o, depths_p]
        loss_dict = compute_ordinal_depth_loss(
            all_masks, silhouettes, depths)
        return loss_dict['depth']

    def physical_factor(self, sample_indices=None) -> torch.Tensor:
        """ We should relate 3D distance to render image size
        so they have similar magnitude.

        d_pixel = d_3d * factor
        loss = d_pixel**2

        Returns:
            factor : (B,) same length as sample_indces
        """
        sample_indices = np.arange(self.bsize) if sample_indices is None else sample_indices
        fx = self.camintr[sample_indices, 0, 0]
        return fx * self.mask_size

    def loss_center(self, obj_idx=0, v_hand=None, v_obj=None) -> torch.Tensor:
        """
        Args:
            obj_idx: int
            verts_hand: (B, V, 3)
            verts_obj: (B, V, 3)

        Return:
            distance between obj and hand center
        """
        # TODO phy_factor
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        center_loss = F.mse_loss(v_hand.mean(1), v_obj[:, obj_idx].mean(1))
        return center_loss

    def loss_chamfer(self,
                     obj_idx=0,
                     v_hand=None,
                     v_obj=None,
                     sample_indices=None) -> torch.Tensor:
        """ returns a scalar """
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        l_chamfer = compute_chamfer_distance(
            v_hand, v_obj[:, obj_idx],
            batch_reduction=None)  # (B,)
        l_chamfer = self.physical_factor(sample_indices=sample_indices) * l_chamfer
        l_chamfer = (l_chamfer**2).sum()
        return l_chamfer

    def loss_nearest_dist(self, obj_idx=0, v_hand=None, v_obj=None) -> torch.Tensor:
        """ returns (B,) """
        # TODO phy_factor
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        l_min_d = compute_nearest_dist(v_obj[:, obj_idx, ...], v_hand)
        return l_min_d

    def loss_contact(self, obj_idx=0, v_hand=None, v_obj=None):
        # TODO phy_factor
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        l_contact = compute_contact_loss(
            verts_hand_b=v_hand,
            verts_object_b=v_obj[:, obj_idx],
            faces_object=self._expand_obj_faces(self.bsize),
            faces_hand=self.faces_hand)
        return l_contact['contact']

    def loss_collision(self, obj_idx=0, v_hand=None, v_obj=None, sample_indices=None):
        """
        Returns:
            (B,)
        """
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        l_collision = compute_collision_loss(
            v_hand, v_obj[:, obj_idx], self._expand_obj_faces(self.bsize))
        l_collision = self.physical_factor(sample_indices=sample_indices) * l_collision
        return l_collision

    """ Contact regions """

    def get_distance_to_prior(self, obj_idx=0, v_hand=None, v_obj=None):
        """
        Returns:
            (B, 5+3)
            squared distance from prior regions to the object.
        """
        raise ValueError("Use loss_closesness")
        k1, k2 = 1, 1
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj

        p2 = v_obj[:, obj_idx]
        loss = v_hand.new_zeros([v_hand.size(0), 8])
        for i, p in enumerate(self.contact_regions.verts):
            p1 = v_hand[:, p, :]
            l = compute_nearest_dist(p1, p2, k1=k1, k2=k2, ret_index=False)
            loss[:, i] = l
        return loss

    def get_normal_to_prior(self, obj_idx=0, v_hand=None, v_obj=None):
        """
        Returns:
            TODO
            squared distance from one tip to the object.
        """
        raise NotImplementedError
        k1, k2 = 1, 1
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        vn_hand = compute_vert_normals(v_hand, faces=self.faces_hand[0])
        vn_obj = compute_vert_normals(v_obj[:, obj_idx], faces=self.faces_object)
        p2 = v_obj[:, obj_idx]
        loss = v_hand.new_zeros([len(v_hand)])

        v1s, v2s, vn1s, vn2s = [], [], [], []
        for p in self.contact_regions.verts:
            p1 = v_hand[:, p, :]
            l, i1, i2 = compute_nearest_dist(
                p1, p2, k1=k1, k2=k2, ret_index=True)
            v1, v2, _, _, vn1, vn2 = find_nearest_vecs(
                p1, p2, k1=k1, k2=k2, pn1=vn_hand, pn2=vn_obj)
            v2 = v2.squeeze_()  # (B, 3)
            vn2 = vn2.squeeze_()  # (B, 3)

            print(v2.shape)
            loss += l
        return loss

    def loss_closeness(self,
                       obj_idx=0, v_hand=None, v_obj=None,
                       squared_dist=False,
                       num_priors=8, reduce_type='avg',
                       sample_indices=None):
        """
        L = distance from finger tips to their nearest vertices
        average over 8(=5+3) regions.
        Options:
            (5 regions vs 8 regions) x (min vs avg)

        Args:
            squared_dist: whether to calc loss as squared distance
            num_priors: 5 or 8
            reduce: 'min' or 'avg'

        Returns:
            loss: (B,)
        """
        if sample_indices is None:
            sample_indices = np.arange(self.bsize)
        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        v_obj = v_obj[:, obj_idx]
        vn_obj = compute_vert_normals(v_obj, faces=self.faces_object)

        ph_idx = reduce(lambda a, b: a + b, self.contact_regions.verts, [])
        ph = v_hand[:, ph_idx, :]
        _, idx, nn = knn_points(ph, v_obj, K=1, return_nn=True)
        nn = nn.squeeze_(2)
        vn_obj_nn = knn_gather(vn_obj, idx).squeeze_(2)

        prod = torch.sum((ph - nn) * vn_obj_nn, dim=-1)  # (B, V_h)
        index = torch.cat(
            [prod.new_zeros(len(v), dtype=torch.long) + i
             for i, v in enumerate(self.contact_regions.verts)])
        regions_min, _ = scatter_min(src=prod, index=index, dim=1)  # (B, 8)
        if squared_dist:
            regions_min = regions_min**2
        else:
            regions_min = regions_min.abs_()
        regions_min = regions_min[:, :num_priors]
        if reduce_type == 'min':
            loss = regions_min.min(dim=-1).values
        elif reduce_type == 'avg':
            loss = regions_min.mean(dim=-1)

        loss *= self.physical_factor(sample_indices=sample_indices)
        return loss

    def loss_insideness(self,
                        v_hand=None, v_obj=None,
                        squared_dist=False,
                        sample_indices=None,
                        num_nearest_points=3,
                        debug_viz=False):
        """
        For all p in object, find nearest K points in hand prior regions,
            compute distance (inner product w/ normal) as loss at this p.
            negative indicate Wrong position.

            Loss = \Avg -1.0 * max(loss_p, 0)
        
        Args:
            num_nearest_points: number of nearest K points in hand

        Returns:
            loss: (B, N_init, V)
        """
        k2 = num_nearest_points
        if sample_indices is None:
            sample_indices = np.arange(self.bsize)

        v_hand = self.get_verts_hand() if v_hand is None else v_hand
        vn_hand = compute_vert_normals(v_hand, faces=self.faces_hand[0])
        v_obj = self.get_verts_object() if v_obj is None else v_obj
        v_obj_size = v_obj.size(-2)
        p_obj = v_obj.view(self.bsize, self.num_obj * v_obj_size, 3)  # 

        p2_idx = reduce(lambda a, b: a + b, self.contact_regions.verts, [])
        p2 = v_hand[:, p2_idx, :]
        vn_hand_part = vn_hand[:, p2_idx, :]  # (B, CONTACT, 3)

        _, idx, nn = knn_points(p_obj, p2, K=k2, return_nn=True)  # idx: (B, N*V, k2), nn: (B, N*V, k2, 3)
        nn_normals = knn_gather(vn_hand_part, idx)  # (B, N*V, k2, 3)

        """ Reshaping """
        p1 = p_obj.view(self.bsize, self.num_obj, v_obj_size, 1, 3).expand(-1, -1, -1, k2, -1)
        nn_normals = nn_normals.view(self.bsize, self.num_obj, v_obj_size, k2, 3)
        nn = nn.view(self.bsize, self.num_obj, v_obj_size, k2, 3)

        vec = (p1 - nn)  # (B, N, V, k2, 3)
        prod = (vec * nn_normals).sum(-1)  # (B, N, V, k2)
        if squared_dist:
            prod = prod**2
        score  = prod.mean(-1)  # (B, N, V)
        loss =  (- score.clamp_max_(0)).mean(-1)  # (B, N)
        phy_factor = self.physical_factor(sample_indices).view(self.bsize, 1)
        loss = loss * phy_factor

        if debug_viz:
            scene = 0
            obj_idx = 0
            vals = score[scene].detach().cpu().numpy()
            mhand, mobj = self.get_meshes(scene, obj_idx)
            colors = np.where(
                vals[..., None] >= 0,
                mobj.visual.vertex_colors,
                trimesh.visual.interpolate(vals, color_map='jet'))
            mobj.visual.vertex_colors = colors
            return trimesh.Scene([mhand, mobj])

        return loss


from typing import List, Tuple
import trimesh
import numpy as np
from libzhifan.geometry import SimpleMesh, visualize_mesh, projection, CameraManager
from libzhifan.geometry import visualize as geo_vis
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use('svg')  # seems svg renders plt faster
import cv2


class HOForwarderV2Vis(HOForwarderV2Impl):
    def __init__(self,
                 vis_rend_size=256,
                 ihoi_img_patch=None,
                 *args,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.vis_rend_size = vis_rend_size
        self.ihoi_img_patch = ihoi_img_patch

    def get_meshes(self, scene_idx, obj_idx, **mesh_kwargs) -> Tuple[SimpleMesh]:
        """
        Args:
            scene_idx: index of scene (timestep)
            obj_idx: -1 indicates no object

        Returns:
            mhand: SimpleMesh
            mobj: SimpleMesh, or None if obj_idx < 0
        """
        hand_color = mesh_kwargs.pop('hand_color', 'light_blue')
        obj_color = mesh_kwargs.pop('obj_color', 'yellow')
        with torch.no_grad():
            verts_hand = self.get_verts_hand(**mesh_kwargs)[scene_idx]
            mhand = SimpleMesh(
                verts_hand, self.faces_hand[scene_idx], tex_color=hand_color)
            if obj_idx < 0:
                mobj = None
            else:
                verts_obj = self.get_verts_object(**mesh_kwargs)[scene_idx, obj_idx]
                mobj = SimpleMesh(
                    verts_obj, self.faces_object, tex_color=obj_color)
        return mhand, mobj

    def finger_with_normals(self, scene_idx,
                            regions=(0,1,2,3,4,5,6,7)) -> trimesh.Scene:
        """
        Returns: a Scene with single hand, finger regions marked with normals.
        """
        hand_color = 'light_blue'
        with torch.no_grad():
            verts_hand = self.get_verts_hand()[scene_idx]
            vn = compute_vert_normals(verts_hand, self.faces_hand[scene_idx])
            mhand = SimpleMesh(
                verts_hand, self.faces_hand[scene_idx], tex_color=hand_color)

        paths = []
        for i, v_inds in enumerate(self.contact_regions.verts):
            if i not in regions:
                continue
            geo_vis.color_verts(mhand, v_inds, (255, 0, 0))

            v_parts = verts_hand[v_inds].cpu().numpy()
            vn_parts = vn[v_inds].cpu().numpy()
            vec = np.column_stack(
                (v_parts, v_parts + (vn_parts * mhand.scale * .05)))
            path = trimesh.load_path(vec.reshape(-1, 2, 3))
            paths.append(path)

        return trimesh.Scene([mhand] + paths)

    def visualize_nearest_normals(self,
                                  scene_idx,
                                  display=('hand', 'obj', 'normals'),
                                  regions=5,
                                  ) -> trimesh.Scene:
        """
        Visualize each region's nearest point to the object
        """
        # Find nearest point indices
        k1, k2 = 1, 1
        obj_idx = 0
        h_paths = []
        o_paths = []
        vh_inds = []
        vo_inds = []
        with torch.no_grad():
            v_hand = self.get_verts_hand()
            v_obj = self.get_verts_object()
            vn_hand = compute_vert_normals(v_hand, faces=self.faces_hand[0])
            vn_obj = compute_vert_normals(v_obj[:, obj_idx], faces=self.faces_object)
            mhand = SimpleMesh(v_hand[scene_idx], self.faces_hand[scene_idx])
            mobj = SimpleMesh(v_obj[scene_idx, obj_idx], self.faces_object)
            p2 = v_obj[:, obj_idx]

            for part in self.contact_regions.verts[:regions]:
                p1 = v_hand[:, part, :]
                pn1 = vn_hand[:, part, :]
                v1, v2, vh_ind, vo_ind, vn1, vn2 = find_nearest_vecs(
                    p1, p2, k1=k1, k2=k2, pn1=pn1, pn2=vn_obj)
                """
                To get index in the hand,
                first get index
                """
                vh_ind = vh_ind[scene_idx].squeeze().item()
                vh_ind = part[vh_ind]
                vh_inds.append( vh_ind )
                vo_inds.append( vo_ind[scene_idx].squeeze().item() )

                v1 = v1[scene_idx].cpu().numpy()
                vn1 = vn1[scene_idx].cpu().numpy()
                v2 = v2.squeeze_(1)[scene_idx].cpu().numpy()  # (1, 3)
                vn2 = vn2.squeeze_(1)[scene_idx].cpu().numpy()  # (1, 3)
                vec1 = np.column_stack(
                    (v1, v1 + (vn1 * mhand.scale * 0.05)))
                vec2 = np.column_stack(
                    (v2, v2 + (vn2 * mobj.scale * 0.05)))
                vec1 = trimesh.load_path(vec1.reshape(-1, 2, 3))
                vec2 = trimesh.load_path(vec2.reshape(-1, 2, 3))
                h_paths.append(vec1)
                o_paths.append(vec2)

            geo_vis.color_verts(mhand, vh_inds, (255, 0, 0))
            geo_vis.color_verts(mobj, vo_inds, (0, 0, 255))

        scene_geoms = []
        if 'hand' in display:
            scene_geoms.append(mhand)
        if 'obj' in display:
            scene_geoms.append(mobj)
        if 'normals' in display:
            if 'hand' in display:
                scene_geoms += h_paths
            if 'obj' in display:
                scene_geoms += o_paths
        return trimesh.Scene(scene_geoms)

    def to_scene(self, scene_idx=-1, obj_idx=0,
                 show_axis=False, viewpoint='nr', **mesh_kwargs):
        """ Returns a trimesh.Scene """
        if scene_idx >= 0:
            mhand, mobj = self.get_meshes(
                scene_idx=scene_idx, obj_idx=obj_idx, **mesh_kwargs)
            return visualize_mesh([mhand, mobj],
                                  show_axis=show_axis,
                                  viewpoint=viewpoint)

        """ Render all """
        hand_color = mesh_kwargs.pop('hand_color', 'light_blue')
        obj_color = mesh_kwargs.pop('obj_color', 'yellow')
        with torch.no_grad():
            verts_hand = self.get_verts_hand(**mesh_kwargs)
            verts_obj = self.get_verts_object(**mesh_kwargs)

        meshes = []
        disp = 0.15  # displacement
        for t in range(self.bsize):
            mhand = SimpleMesh(
                verts_hand[t], self.faces_hand[t], tex_color=hand_color)
            mhand.apply_translation_([t * disp, 0, 0])
            mobj = SimpleMesh(
                verts_obj[t, obj_idx], self.faces_object, tex_color=obj_color)
            mobj.apply_translation_([t * disp, 0, 0])
            meshes.append(mhand)
            meshes.append(mobj)
        return visualize_mesh(meshes, show_axis=show_axis, viewpoint=viewpoint)

    def render_summary(self, scene_idx, obj_idx=0) -> np.ndarray:
        a1 = np.uint8(self.ihoi_img_patch[scene_idx])
        mask_hand = self.ref_mask_hand[scene_idx].cpu().numpy().squeeze()
        mask_obj = self.ref_mask_object[scene_idx].cpu().numpy()
        all_mask = np.zeros_like(a1, dtype=np.float32)
        all_mask = np.where(
            mask_hand[...,None], (0, 0, 0.8), all_mask)
        all_mask = np.where(
            mask_obj[...,None], (0.6, 0, 0), all_mask)
        all_mask = np.uint8(255*all_mask)
        a2 = cv2.addWeighted(a1, 0.9, all_mask, 0.5, 1.0)
        a3 = np.uint8(self.render_scene(scene_idx=scene_idx, obj_idx=obj_idx)*255)
        b = np.uint8(255*self.render_triview(scene_idx=scene_idx, obj_idx=obj_idx))
        a = np.hstack([a3, a2, a1])
        return np.vstack([a,
                          b])

    def render_scene(self, scene_idx, obj_idx=0,
                     with_hand=True, overlay_gt=False,
                     **mesh_kwargs) -> np.ndarray:
        """ returns: (H, W, 3) """
        if not with_hand:
            return self.ihoi_img_patch[scene_idx]
        mhand, mobj = self.get_meshes(
            scene_idx=scene_idx, obj_idx=obj_idx, **mesh_kwargs)
        img = projection.perspective_projection_by_camera(
            [mhand, mobj],
            CameraManager.from_nr(
                self.camintr.detach().cpu().numpy()[scene_idx], self.vis_rend_size),
            method=dict(
                name='pytorch3d',
                coor_sys='nr',
                in_ndc=False
            ),
            image=self.ihoi_img_patch[scene_idx],
        )

        if overlay_gt:
            all_mask = np.zeros_like(img, dtype=np.float32)
            mask_hand = self.ref_mask_hand[scene_idx].cpu().numpy().squeeze()
            all_mask = np.where(
                mask_hand[...,None], (0, 0, 0.8), all_mask)
            if obj_idx >= 0:
                mask_obj = self.ref_mask_object[scene_idx].cpu().numpy()
                all_mask = np.where(
                    mask_obj[...,None], (0.6, 0, 0), all_mask)
            all_mask = np.uint8(255*all_mask)
            img = cv2.addWeighted(np.uint8(img*255), 0.9, all_mask, 0.5, 1.0)
        return img

    def render_triview(self, scene_idx, obj_idx=0, **mesh_kwargs) -> np.ndarray:
        """
        Returns:
            (H, W, 3)
        """
        rend_size = 256
        mhand, mobj = self.get_meshes(scene_idx=scene_idx, obj_idx=obj_idx, **mesh_kwargs)
        front = projection.project_standardized(
            [mhand, mobj],
            direction='+z',
            image_size=rend_size,
            method=dict(
                name='pytorch3d',
                coor_sys='nr',
                in_ndc=False
            )
        )
        left = projection.project_standardized(
            [mhand, mobj],
            direction='+x',
            image_size=rend_size,
            method=dict(
                name='pytorch3d',
                coor_sys='nr',
                in_ndc=False
            )
        )
        back = projection.project_standardized(
            [mhand, mobj],
            direction='-z',
            image_size=rend_size,
            method=dict(
                name='pytorch3d',
                coor_sys='nr',
                in_ndc=False
            )
        )
        return np.hstack([front, left, back])

    def render_grid_np(self, obj_idx=0, with_hand=True,
                       sample_indices=None, *args, **kwargs) -> np.ndarray:
        """ low resolution but faster """
        l = self.bsize
        num_cols = 5
        num_rows = (l + num_cols - 1) // num_cols
        imgs = []
        for cam_idx in range(l):
            img = self.render_scene(
                scene_idx=cam_idx, obj_idx=obj_idx,
                with_hand=with_hand, *args, **kwargs)
            imgs.append(img)
            if cam_idx == l-1:
                break

        h, w, _ = imgs[0].shape
        out = np.empty(shape=(num_rows*h, num_cols*w, 3), dtype=imgs[0].dtype)
        if sample_indices is None:
            sample_indices = np.arange(self.bsize)
        sample_indices = set(sample_indices)
        for row in range(num_rows):
            for col in range(num_cols):
                idx = row*num_cols+col
                if  idx >= l:
                    break
                out[row*h:(row+1)*h, col*w:(col+1)*w, :] = imgs[idx]
                # Draw red bounding box
                if idx in sample_indices:
                    out = cv2.rectangle(
                        out, (col*w, row*h), ((col+1)*w, (row+1)*h),
                        (1, 0, 0), thickness=8)

        return out

    def render_grid(self, obj_idx=0, with_hand=True,
                    figsize=(10, 10), low_reso=True, *args, **kwargs):
        """ grip of  multiple render """
        if low_reso:
            out = self.render_grid_np(obj_idx=obj_idx, with_hand=with_hand, *args, **kwargs)
            fig, ax = plt.subplots()
            ax.imshow(out)
            plt.axis('off')
            return fig

        l = self.bsize
        num_cols = 5
        num_rows = (l + num_cols - 1) // num_cols
        fig, axes = plt.subplots(
            nrows=num_rows, ncols=num_cols,
            sharex=True, sharey=True, figsize=figsize)
        for cam_idx, ax in enumerate(axes.flat, start=0):
            if cam_idx > l-1:
                ax.set_axis_off()
                continue
            img = self.render_scene(
                scene_idx=cam_idx, obj_idx=obj_idx, with_hand=with_hand, *args, **kwargs)
            ax.imshow(img)
            ax.set_axis_off()
        plt.tight_layout()
        return fig

    def render_global(self,
                      global_cam: BatchCameraManager,
                      global_images: np.ndarray,
                      scene_idx: int,
                      obj_idx=0,
                      with_hand=True,
                      overlay_gt=False,
                      ) -> np.ndarray:
        """ returns: (H, W, 3) """
        if not with_hand:
            return self.ihoi_img_patch[scene_idx]
        mhand, mobj = self.get_meshes(
            scene_idx=scene_idx, obj_idx=obj_idx)
        img = projection.perspective_projection_by_camera(
            [mhand, mobj],
            global_cam[scene_idx],
            method=dict(
                name='pytorch3d',
                coor_sys='nr',
                in_ndc=False
            ),
            image=global_images[scene_idx],
        )

        if overlay_gt:
            all_mask = np.zeros_like(img, dtype=np.float32)
            mask_hand = self.ref_mask_hand[scene_idx].cpu().numpy().squeeze()
            all_mask = np.where(
                mask_hand[...,None], (0, 0, 0.8), all_mask)
            if obj_idx >= 0:
                mask_obj = self.ref_mask_object[scene_idx].cpu().numpy()
                all_mask = np.where(
                    mask_obj[...,None], (0.6, 0, 0), all_mask)
            all_mask = np.uint8(255*all_mask)
            img = cv2.addWeighted(np.uint8(img*255), 0.9, all_mask, 0.5, 1.0)
        return img
