from typing import List
import matplotlib.pyplot as plt
import numpy as np
from homan.ho_forwarder_v2 import HOForwarderV2Vis

from libzhifan.geometry import visualize_mesh, SimpleMesh
from libzhifan.geometry import BatchCameraManager


def plot_pose_summaries(pose_machine,
                        pose_idx=0) -> plt.figure:
    """ homans: list of HO_forwarder 
    Args:
        pose_machine: obj_pose.pose_optimizer.PoseOptimizer
    """
    l = len(pose_machine.global_cam)
    num_cols = 5
    num_rows = (l + num_cols - 1) // num_cols
    fig, axes = plt.subplots(
        nrows=num_rows, ncols=num_cols,
        sharex=True, sharey=True, figsize=(20, 20))
    for cam_idx, ax in enumerate(axes.flat, start=0):
        img = pose_machine.render_model_output(
            pose_idx, cam_idx=cam_idx, kind='ihoi',
            with_obj=True)
        ax.imshow(img)
        ax.set_axis_off()
        if cam_idx == l-1:
            break

    plt.tight_layout()
    return fig


def concat_pose_meshes(pose_machine,
                       pose_idx=0,
                       obj_file=None):
    """
    Returns a list of SimpleMesh,
    offset each timestep for easier comparison?
    Args:
        pose_machine: obj_pose.pose_optimizer.PoseOptimizer
    """
    meshes = []
    l = len(pose_machine.global_cam)
    obj_verts = pose_machine.pose_model.fitted_results.verts
    disp = 0.15  # displacement
    for cam_idx in range(l):
        hand_mesh = pose_machine.hand_simplemesh(cam_idx=cam_idx)
        obj_mesh = SimpleMesh(
            obj_verts[cam_idx, pose_idx],
            pose_machine.pose_model.faces,
            tex_color='yellow')
        hand_mesh.apply_translation_([cam_idx * disp, 0, 0])
        obj_mesh.apply_translation_([cam_idx * disp, 0, 0])
        meshes.append(hand_mesh)
        meshes.append(obj_mesh)

    if obj_file is not None:
        visualize_mesh(meshes, show_axis=False).export(
            obj_file)
    return meshes


def make_compare_video(homan: HOForwarderV2Vis,
                       global_cam: BatchCameraManager,
                       global_images: np.ndarray,
                       render_frames: str) -> List[np.ndarray]:
    """
    Args:
        frames: 'all' or 'ransac'
    """
    frames = []
    if render_frames == 'all':
        scene_indices = range(homan.bsize)
    elif render_frames == 'ransac':
        scene_indices = homan.sample_indices

    for i in scene_indices:
        img_mesh = homan.render_global(
            global_cam=global_cam,
            global_images=global_images,
            scene_idx=i,
            obj_idx=0,
            with_hand=True,
            overlay_gt=False)
        img = np.vstack([global_images[i], img_mesh * 255])
        frames.append(img)
    return frames