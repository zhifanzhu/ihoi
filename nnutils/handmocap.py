from typing import Tuple
import os.path as osp
import torch

import pytorch3d.transforms.rotation_conversions as rot_cvt
from handmocap.hand_mocap_api import HandMocap
from handmocap.hand_bbox_detector import HandBboxDetector

from nnutils import geom_utils
from nnutils.hand_utils import ManopthWrapper

from libzhifan.geometry import BatchCameraManager

from config.epic_constants import WEAK_CAM_FX, IMG_HEIGHT, IMG_WIDTH


""" ManopthWrapper"""

__hand_wrapper_left = ManopthWrapper(flat_hand_mean=False, side='left').to('cuda')
__hand_wrapper_right = ManopthWrapper(flat_hand_mean=False, side='right').to('cuda')


def get_hand_wrapper(side: str) -> ManopthWrapper:
    if 'left' in side:
        return __hand_wrapper_left
    elif 'right' in side:
        return __hand_wrapper_right
    else:
        raise ValueError(f"Side {side} not understood.")


def recover_pca_pose(pred_hand_pose: torch.Tensor, side: str) -> torch.Tensor:
    """
    if
        v_exp = ManopthWrapper(pca=False, flat=False).(x_0)
        x_pca = self.recover_pca_pose(self.x_0)  # R^45
    then
        v_act = ManoLayer(pca=True, flat=False, ncomps=45).forward(x_pca)
        v_exp == v_act

    note above requires mano_rot == zeros, since the computation of rotation
        is different in ManopthWrapper
    """
    M_pca_inv = torch.inverse(
        get_hand_wrapper(side).mano_layer_side.th_comps)
    mano_pca_pose = pred_hand_pose.mm(M_pca_inv)
    return mano_pca_pose


def get_hand_faces(side: str) -> torch.Tensor:
    return get_hand_wrapper(side).hand_faces


""" HandMocap and HandBboxDetector"""

def get_handmocap_predictor(
        mocap_dir='externals/frankmocap',
        checkpoint_hand='extra_data/hand_module/pretrained_weights/pose_shape_best.pth', 
        smpl_dir='extra_data/smpl/',
    ):
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    hand_mocap = HandMocap(osp.join(mocap_dir, checkpoint_hand), 
        osp.join(mocap_dir, smpl_dir), device = device)
    return hand_mocap


def collate_mocap_hand(mocap_predictions: list,
                       side: str,
                       fields=('pred_hand_pose', 'pred_hand_betas',
                               'pred_camera', 'bbox_processed')
                       ) -> dict:
    """
    mocap shapes:
        pred_vertices_smpl (778, 3)
        pred_joints_smpl (21, 3)
        faces (1538, 3)
        bbox_scale_ratio ()
        bbox_top_left (2,)
        bbox_processed (4,)
        pred_camera (3,)
        img_cropped (224, 224, 3)
        pred_hand_pose (1, 48)
        pred_hand_betas (1, 10)
        pred_vertices_img (778, 3)
        pred_joints_img (21, 3)
    
    Args:
        mocap_predictions: list of [
            dict('left_hand': dict
                 'right_hand': dict) 
            ]
    
    Returns:
        mocap_hand: dict with key in `fields`.
    """
    one_hand = dict()
    for key in fields:
        content = []
        for mocap_pred in mocap_predictions:
            elem = torch.as_tensor(mocap_pred[side][key])
            if key != 'pred_hand_pose' and key != 'pred_hand_betas':
                elem = elem.unsqueeze(0)
            content.append(elem)
        content = torch.cat(content, dim=0)
        one_hand[key] = content
    return one_hand


def get_handmocap_detector(view_type='ego_centric'):
    device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
    bbox_detector =  HandBboxDetector(view_type, device)
    return bbox_detector


""" Used by `obj_pose` """


def compute_hand_transform(rot_axisang, 
                           pred_hand_pose, 
                           pred_camera,
                           side: str):
    """
    Args:
        rot_axisang: (B, 3)
        pred_hand_pose: (B, 45)
        pred_camera: (B, 3)
            Used for translate hand_mesh to convert hand_mesh
            so that result in a weak perspective camera.
        hand_wrapper: ManoPthWrapper

    Returns:
        rotation: (B, 3, 3) row-vec
        translation: (B, 1, 3)
    """
    rotation = rot_cvt.axis_angle_to_matrix(rot_axisang)  # (1, 3) - > (1, 3, 3)
    rot_homo = geom_utils.rt_to_homo(rotation)
    glb_rot = geom_utils.matrix_to_se3(rot_homo)  # (1, 4, 4) -> (1, 12)
    _, joints = get_hand_wrapper(side)(
        glb_rot,
        pred_hand_pose, return_mesh=True)
    fx = WEAK_CAM_FX
    s, tx, ty = torch.split(pred_camera, [1, 1, 1], dim=1)
    translate = torch.cat([tx, ty, fx/s], dim=1)
    translation = translate - joints[:, 5]
    rotation_row = rotation.transpose(1, 2)
    return rotation_row, translation[:, None]


def cam_from_bbox(hand_bbox,
                  fx=WEAK_CAM_FX,
                  img_height=IMG_HEIGHT,
                  img_width=IMG_WIDTH) -> Tuple[BatchCameraManager, BatchCameraManager]:
    """
    Args:
        hand_bbox: (B, 4) in GLOBAL screen space
            This box should be used in mocap_predictor.
            hand bounding box XYWH in original image
            same as one_hand['bbox_processed']
    
    Returns:
        hand_cam, global_cam: BatchCameraManager
    """
    hand_crop_h = 224
    hand_crop_w = 224
    _, _, box_w, box_h = torch.split(hand_bbox, [1, 1, 1, 1], dim=1)
    box_h = box_h.view(-1)
    box_w = box_w.view(-1)
    fx = torch.ones_like(box_w) * fx
    fy = torch.ones_like(box_w) * fx
    cx = torch.zeros_like(box_w)
    cy = torch.zeros_like(box_w)
    hand_crop_h = torch.ones_like(box_w) * hand_crop_h
    hand_crop_w = torch.ones_like(box_w) * hand_crop_w
    hand_cam = BatchCameraManager(
        fx=fx, fy=fy, cx=cx, cy=cy, img_h=box_h, img_w=box_w,
        in_ndc=True
    ).resize(hand_crop_h, hand_crop_w)

    _, _, hand_h, hand_w = torch.split(hand_bbox, [1, 1, 1, 1], dim=1)
    hand_h = hand_h.view(-1)
    hand_w = hand_w.view(-1)
    global_cam = hand_cam.resize(hand_h, hand_w).uncrop(
        hand_bbox, img_height, img_width)
    return hand_cam, global_cam