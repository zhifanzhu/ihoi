from typing import NamedTuple, Union
import tqdm
import numpy as np
import torch
from homan.ho_forwarder_v2 import HOForwarderV2Vis, HOForwarderV2Impl
from homan.utils.geometry import rot6d_to_matrix
from temporal.utils import choose_with_softmax, init_6d_pose_from_bboxes

from moviepy import editor


""" Different HO optimization plans. """


def smooth_hand_pose(homan: HOForwarderV2Impl,
                     lr=1e-2,
                     thresh=1e-3,
                     verbose=True):
    """ smooth pca pose """
    optimizer = torch.optim.Adam([
        {
            'params': [
                homan.mano_pca_pose
            ],
            'lr': lr
        }
    ])

    max_steps = 500
    for _ in range(max_steps):
        optimizer.zero_grad()
        pca_loss = homan.loss_pca_interpolation().sum()
        if pca_loss < thresh:
            break
        pca_loss.backward()
        optimizer.step()

    return homan


def optimize_hand(homan: HOForwarderV2Impl,
                  lr=1e-2,
                  num_steps=100,
                  verbose=True) -> HOForwarderV2Impl:
    optimizer = torch.optim.Adam([
        {
            'params': [
                homan.rotations_hand,
                homan.translations_hand,
            ],
            'lr': lr
        }
    ])

    loss_records = {
        'total': [],
        'sil': [],
        'pca': [],
        'rot': [],
        'transl': [],
    }
    for step in range(num_steps):
        optimizer.zero_grad()
        tot_loss, loss_dict = homan.forward_hand()
        if verbose and step % 10 == 0:
            print(f"Step {step}, tot = {tot_loss.item():.04f}, ", end=' ')
            for k, v in loss_dict.items():
                print(f"{k} = {v.item():.04f}", end=' ')
            print()
        loss_records['total'].append(tot_loss.item())
        for k, v in loss_dict.items():
            loss_records[k].append(v.item())

        tot_loss.backward()
        optimizer.step()

    homan.loss_records = loss_records

    return homan


class ObjectParams(NamedTuple):
    translations_object: torch.Tensor
    rotations_object: torch.Tensor
    scale_object: torch.Tensor


def sampled_obj_optimize(homan: HOForwarderV2Vis,
                         lr=1e-2,
                         num_epochs=50,
                         num_iters=2000,
                         temperature=100.,
                         ratio=0.5,
                         with_contact=True,
                         weights=None,
                         vis_interval=-1,
                         save_grid: str = None):
    """
    Args:
        save_grid: str, fname to save the optimization process
    """

    if hasattr(homan, 'info'):
        info = homan.info
        prefix = f'{info.vid}_{info.gt_frame}'

    optimizer = torch.optim.Adam([
        {
            'params': [
                homan.rotations_object,  # (1,)
                homan.translations_object,
                # homan.scale_object,
            ],
            'lr': lr
        }
    ])

    weights = homan.rotations_hand.new_zeros([homan.bsize]) if weights is None else weights
    if save_grid:
        out_frames = []

    for e in range(num_epochs):

        sample_indices = choose_with_softmax(
            weights, temperature=temperature, ratio=ratio)
        # print(f"Sample {sample_indices} at epoch {e}, weights = {weights.tolist()}")

        with tqdm.tqdm(total=num_iters) as loop:
            for step in range(num_iters):
                optimizer.zero_grad()

                v_hand = homan.get_verts_hand()[sample_indices, ...]
                v_obj = homan.get_verts_object()[sample_indices, ...]

                l_obj_dict = homan.forward_obj_pose_render(
                    sample_indices=sample_indices)  # (B,N)
                l_obj_mask = l_obj_dict['mask']
                l_inside = homan.loss_insideness(
                    v_hand=v_hand, v_obj=v_obj, sample_indices=sample_indices)
                l_inside = l_inside.sum()
                min_dist = homan.loss_nearest_dist(v_hand=v_hand, v_obj=v_obj).min()

                # Accumulate
                l_obj_mask = l_obj_mask.sum()
                if with_contact:
                    tot_loss = l_obj_mask + l_inside
                else:
                    tot_loss = l_obj_mask

                if save_grid and step % 5 == 0:
                    frame = homan.render_grid_np(0, True, sample_indices)
                    out_frames.append(frame)

                if vis_interval > 0 and step % vis_interval == 0:
                    print(
                        f"obj_mask:{l_obj_mask.item():.3f} "
                        f"inside:{l_inside.item():.3f} "
                        f"min_dist: {min_dist:.3f} "
                        )

                tot_loss.backward()
                optimizer.step()
                loop.set_description(f"tot_loss: {tot_loss.item():.3g}")
                loop.update()

        # Update weights
        weights[sample_indices] -= tot_loss

    if save_grid:
        editor.ImageSequenceClip(
            [v*255 for v in out_frames], fps=15).write_videofile(save_grid)

    return homan, weights


def reinit_sample_optimize(homan: HOForwarderV2Vis,
                           global_cam=None,
                           lr=1e-2,
                           num_epochs=50,
                           num_iters=2000,
                           temperature=100.,
                           ratio=0.5,
                           with_contact=True,
                           weights=None,
                           vis_interval=-1,
                           save_grid: str = None,
                           ):
    """
    Args:
        save_grid: str, fname to save the optimization process
    """

    weights = homan.rotations_hand.new_zeros([homan.bsize]) if weights is None else weights
    if save_grid:
        out_frames = []

    rotations, _ = init_6d_pose_from_bboxes(
        None, homan.get_verts_object()[0, 0], cam_mat=global_cam,
        num_init=num_epochs,
        base_rotation=rot6d_to_matrix(homan.rotations_hand),
        base_translation=homan.translations_hand,
        zero_init_transl=True)

    best_val_score = homan.rotations_hand.new_tensor(np.inf)
    results = []

    for e in range(num_epochs):

        sample_indices = choose_with_softmax(
            weights, temperature=temperature, ratio=ratio)
        # print(f"Sample {sample_indices} at epoch {e}, weights = {weights.tolist()}")

        homan.set_obj_transform(
            homan.translations_object,
            rotations[e],
            homan.translations_object.new_ones([1]))
        optimizer = torch.optim.Adam([{
            'params': [
                homan.rotations_object,  # (1,)
                homan.translations_object,
                homan.scale_object,
            ],
            'lr': lr
        }])

        with tqdm.tqdm(total=num_iters) as loop:
            for step in range(num_iters):
                optimizer.zero_grad()

                v_hand = homan.get_verts_hand()[sample_indices, ...]
                v_obj = homan.get_verts_object()[sample_indices, ...]

                l_obj_dict = homan.forward_obj_pose_render(
                    sample_indices=sample_indices)  # (B,N)
                l_obj_mask = l_obj_dict['mask']
                l_inside = homan.loss_insideness(
                    v_hand=v_hand, v_obj=v_obj, sample_indices=sample_indices)
                l_inside = l_inside.sum()
                min_dist = homan.loss_nearest_dist(v_hand=v_hand, v_obj=v_obj).min()

                # Accumulate
                l_obj_mask = l_obj_mask.sum()
                if with_contact:
                    tot_loss = l_obj_mask  + l_inside
                else:
                    tot_loss = l_obj_mask

                if save_grid and step % 5 == 0:
                    frame = homan.render_grid_np(0, True, sample_indices)
                    out_frames.append(frame)

                if vis_interval > 0 and step % vis_interval == 0:
                    print(
                        f"obj_mask:{l_obj_mask.item():.3f} "
                        f"inside:{l_inside.item():.3f} "
                        f"min_dist: {min_dist:.3f} "
                        )

                tot_loss.backward()
                optimizer.step()
                loop.set_description(f"tot_loss: {tot_loss.item():.3g}")
                loop.update()

        mask_score = homan.forward_obj_pose_render(sample_indices=sample_indices)['mask'].sum()
        collision_score = homan.loss_collision(
            v_hand=v_hand, v_obj=v_obj, sample_indices=sample_indices).sum()
        inside_score = homan.loss_insideness(
            v_hand=v_hand, v_obj=v_obj, sample_indices=sample_indices).sum()
        val_error = collision_score + mask_score
        R = homan.rotations_object.detach().clone()
        t = homan.translations_object.detach().clone()
        s = homan.scale_object.detach().clone()
        element = (
            mask_score.item(), collision_score.item(), inside_score.item(),
            R, t, s)
        results.append(element)
        # Update weights
        weights[sample_indices] -= tot_loss

    if save_grid:
        editor.ImageSequenceClip(
            [v*255 for v in out_frames], fps=15).write_videofile(save_grid)

    # write-back best
    # Choose best
    final_score = \
        torch.softmax(torch.as_tensor([-v[0] for v in results]), 0) + \
        torch.softmax(torch.as_tensor([-v[1] for v in results]), 0) + \
        torch.softmax(torch.as_tensor([-v[2] for v in results]), 0)
    idx = final_score.argmax()
    R, t, s = results[idx][3], results[idx][4], results[idx][5]

    homan.set_obj_transform(
        translations_object=t,
        rotations_object=R,
        scale_object=s)

    return homan, weights, results
