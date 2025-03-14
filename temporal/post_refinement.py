from argparse import ArgumentParser
from tqdm import tqdm
import os.path as osp
from moviepy import editor

import torch
from datasets.epic_clip_v3 import EpicClipDatasetV3
from homan.mvho_forwarder import MVHOVis, LiteHandModule
from homan.ho_forwarder_v2 import HOForwarderV2Vis
from omegaconf import OmegaConf
from temporal.optim_multiview import EvalHelper
from nnutils.handmocap import extract_forwarder_input
from temporal.visualize import make_compare_video

from libzhifan import io


def parse_args():
    parser = ArgumentParser()
    parser.add_argument('--data_locate')
    parser.add_argument('--model_dir')
    # parser.add_argument('--lw_smooth', type=float, default=1.0)
    return parser.parse_args()


def optimize_post(homan, steps=200, optim_trans_hand=True):
    loss_lim = torch.tensor(1e5)
    def loss_combined(homan):
        l_ho = homan.forward_combined_sil().sum()
        l_obj_sm = homan.loss_obj_smooth().sum()
        # l_hand_sm = homan.loss_hand_smooth().sum()
        return l_ho + l_obj_sm

    def loss_mano_pca(homan: HOForwarderV2Vis):
        l_pca = homan.loss_pca_interpolation().sum()
        l_in = homan.loss_insideness().sum()
        l_cl = homan.loss_closeness().sum()
        l_vhand = homan.loss_hand_smooth(hand_space=True, phy_factor=False).sum()
        l = l_pca + l_in + l_cl + l_vhand
        return l

    """ [R|T] for hand """
    if optim_trans_hand:
        params = [
            homan.rotations_hand,
            homan.translations_hand
        ]
    else:
        params = [
            homan.rotations_hand
        ]
    optim = torch.optim.Adam([{
        'params': params,
        'lr': 1e-2
    }])
    with tqdm(total=steps) as loop:
        for i in range(steps):
            optim.zero_grad()
            loss = loss_combined(homan)
            if loss > loss_lim:
                break
            loss.backward()
            loop.set_description(f"loss: {loss.item():.3g}")
            loop.update()
            optim.step()
    
    """ mano_pca_pose """
    optim = torch.optim.Adam([{
        'params': [
            homan.mano_pca_pose
        ],
        'lr': 1e-2
    }])
    with tqdm(total=steps) as loop:
        for i in range(steps):
            optim.zero_grad()
            loss = loss_mano_pca(homan)
            if loss > loss_lim:
                break
            loss.backward()
            loop.set_description(f"loss: {loss.item():.3g}")
            loop.update()
            optim.step()
        return homan


def load_homan_from_mvho(eval_input, mvho, cfg, 
                         mano_pca_pose=None,
                         mano_betas=None) -> HOForwarderV2Vis:
    images, hand_bbox_dicts, side, obj_bboxes, hand_masks, obj_masks, cat, global_cam = eval_input

    eval_ihoi_cam_nr_mat, eval_ihoi_cam_mat, eval_image_patch, \
    eval_hand_rotation_6d, eval_hand_translation, \
    eval_mano_pca_pose, eval_pred_hand_betas, eval_hand_mask_patch, eval_obj_mask_patch = \
        extract_forwarder_input(
            eval_input, ihoi_box_expand=cfg.preprocess.ihoi_box_expand)
    num_eval = min(cfg.optim_mv.num_eval, len(eval_ihoi_cam_nr_mat))

    mano_pca_pose = mano_pca_pose if mano_pca_pose is not None else eval_mano_pca_pose
    mano_betas = mano_betas if mano_betas is not None else eval_pred_hand_betas
    homan = HOForwarderV2Vis(
        camintr=eval_ihoi_cam_nr_mat,
        ihoi_img_patch=eval_image_patch)
    homan.set_hand_params(
        rotations_hand=mvho.rotations_hand, #eval_hand_rotation_6d,
        translations_hand=mvho.translations_hand, #eval_hand_translation,
        hand_side=side,
        mano_pca_pose=mano_pca_pose,
        mano_betas=mano_betas)
    homan.set_hand_target(eval_hand_mask_patch)

    homan.set_obj_params(
        translations_object=mvho.translations_object,
        rotations_object=mvho.rotations_object,
        verts_object_og=mvho.verts_object_og,
        faces_object=mvho.faces_object,
        scale_mode='scalar', scale_init=mvho.scale_object
    )
    homan.set_obj_target(eval_obj_mask_patch)
    return homan


def main(args):
    model_dir = args.model_dir
    data_locate = args.data_locate
    cfg = OmegaConf.load('config/conf_multiview.yaml')

    image_sets = '/home/skynet/Zhifan/epic_analysis/hos/tools/model-input-Feb03.json'
    image_sets = '/home/skynet/Zhifan/epic_analysis/hos/tools/eval_100_Feb25.json'
    image_sets = '/home/skynet/Zhifan/epic_analysis/hos/tools/eval_rand100_Mar05.json'
    eval_dataset = EpicClipDatasetV3(
        image_sets=image_sets, sample_frames=30,
        show_loading_time=True
    )
    index = eval_dataset.locate_index_from_output(data_locate)

    vid_key = '_'.join(data_locate.split('_')[:4])
    fmt = osp.join(model_dir, f'{vid_key}_%s')
    mvho_path = fmt % 'model.pth'
    mvho = torch.load(mvho_path)
    eval_input = eval_dataset[index]

    eval_helper = EvalHelper()
    eval_helper.set_eval_data(eval_dataset, index, cfg, eval_input.side,
                              optimize_eval_hand=cfg.homan.optimize_eval_hand)
    homan = load_homan_from_mvho(eval_input, mvho, cfg,
                                 mano_pca_pose=eval_helper.eval_mano_pca_pose,
                                 mano_betas=eval_helper.eval_mano_betas)

    homan.register_combined_target()
    pre_metrics = homan.eval_metrics(unsafe=True, avg=True)
    homan = optimize_post(homan, steps=200, optim_trans_hand=False)
    post_metrics = homan.eval_metrics(unsafe=True, avg=True)

    torch.save(homan, (fmt % 'post.pth'))
    io.write_json(pre_metrics, (fmt % 'pre.json'))
    io.write_json(post_metrics, (fmt % 'post.json'))

    if cfg.action_video.save:
        frames = make_compare_video(
            homan, eval_input.global_camera, global_images=eval_input.images,
            render_frames='all')
        action_cilp = editor.ImageSequenceClip(frames, fps=5)
        action_cilp.write_videofile(fmt % 'post.mp4')


if __name__ == '__main__':
    main(parse_args())
