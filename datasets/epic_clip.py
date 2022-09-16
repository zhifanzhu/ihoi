from typing import NamedTuple, List
import pickle
import json
import os.path as osp
import numpy as np
import torch

from torch.utils.data import Dataset

from config.epic_constants import HAND_MASK_KEEP_EXPAND
from nnutils.image_utils import square_bbox
from datasets.epic_lib.epic_utils import (
    read_epic_image, read_mask_with_occlusion)

from libzhifan.odlib import xyxy_to_xywh, xywh_to_xyxy
from libzhifan.geometry import CameraManager, BatchCameraManager

# Visualization
import matplotlib.pyplot as plt
from libzhifan import odlib
odlib.setup('xywh')
import cv2


epic_cats = [
    '_bg',
    'left hand',
    'right hand',
    'can',
    'cup',
    'plate',
    'bottle',
    'mug',
    'bowl',
]


def row2xywh(row):
    wid = row.right - row.left
    hei = row.bottom - row.top
    return np.asarray([row.left, row.top, wid, hei])


class ClipInfo(NamedTuple):
    vid: str
    gt_frame: str
    cat: str
    side: str
    start: int
    end: int
    comments: str


class DataElement(NamedTuple):
    images: list 
    hand_bbox_dicts: list 
    side_return: str 
    obj_bboxes: torch.Tensor
    hand_masks: torch.Tensor 
    object_masks: torch.Tensor 
    cat: str


class EpicClipDataset(Dataset):

    wrong_set = {
        # Hand box missing/wrong-side in hoa
        ('P04_13', 10440),
        ('P11_16', 18079),
        ('P12_04', 1828),
        ('P12_101', 21783),
        ('P15_02', 25465),
        ('P22_107', 6292),
        ('P28_109', 10026),
        ('P37_101', 70106),
        # object box no merged
        ('P01_103', 538),
        ('P03_04', 43470),
        ('P11_16', 16029),
        ('P37_101', 15996),
    }

    def __init__(self,
                 image_sets='/home/skynet/Zhifan/data/epic_analysis/gt_clips.json',
                 epic_root='/home/skynet/Zhifan/datasets/epic',
                 mask_dir='/home/skynet/Zhifan/data/epic_analysis/InterpV2',
                 all_boxes='/home/skynet/Zhifan/data/epic_analysis/clip_boxes.pkl',
                 image_size=(1280, 720), # (640, 360),
                 hand_expansion=0.4,
                 crop_hand_mask=True,
                 sample_frames=20,
                 *args,
                 **kwargs):
        """_summary_

        Args:
            image_sets (str): path to clean set frames
            epic_root (str):
            hoa_root (str):
            mask_dir (str):
            image_size: Tuple of (W, H)
            hand_expansion (float): size of hand bounding box after squared.
            crop_hand_mask: If True, will crop hand mask with only pixels
                inside hand_bbox.
            sample_frames (int):
                If clip has frames more than sample_frames,
                subsample them to a reduced number.
        """
        super().__init__(*args, **kwargs)
        self.epic_rgb_root = osp.join(epic_root, 'rgb_root')
        self.mask_dir = mask_dir
        self.hoa_root = osp.join(epic_root, 'hoa')
        self.image_size = image_size
        self.hand_expansion = hand_expansion
        self.crop_hand_mask = crop_hand_mask
        self.sample_frames = sample_frames

        self.box_scale = np.asarray(image_size * 2) / ((1920, 1080) * 2)
        self.data_infos = self._read_image_sets(image_sets)
        with open(all_boxes, 'rb') as fp:
            self.ho_boxes = pickle.load(fp)

    def _read_image_sets(self, image_sets) -> List[ClipInfo]:
        """
        Some clips with wrong bounding boxes are deleted;
        Clips with comments (usually challenging ones) are deleted.

        Returns:
            list of ClipInfo(vid, nid, frame_idx, cat, side, start, end)
        """
        with open(image_sets) as fp:
            infos = json.load(fp)

        infos = [ClipInfo(**v)
                 for v in infos
                 if (v['vid'], v['gt_frame']) not in self.wrong_set]
        infos = [v for v in infos if len(v.comments) == 0]
        return infos

    def __len__(self):
        return len(self.data_infos)

    def _get_hand_box(self, vid, frame_idx, side, expand=True):
        hand_box = self.ho_boxes[vid][frame_idx][side]
        if not expand:
            return hand_box
        hand_box_xyxy = xywh_to_xyxy(hand_box)
        hand_box_squared_xyxy = square_bbox(
            hand_box_xyxy[None], pad=self.hand_expansion)[0]
        w, h = self.image_size
        hand_box_squared_xyxy[:2] = hand_box_squared_xyxy[:2].clip(min=[0, 0])
        hand_box_squared_xyxy[2:] = hand_box_squared_xyxy[2:].clip(max=[w, h])
        hand_box_squared = xyxy_to_xywh(hand_box_squared_xyxy)
        return hand_box_squared

    def _get_obj_box(self, vid, frame_idx, cat):
        return self.ho_boxes[vid][frame_idx][cat]

    def visualize_bboxes(self, index):
        images, hand_bbox_dicts, side, obj_bboxes, hand_masks, obj_masks, _ \
            = self.__getitem__(index)
        l = len(images)
        num_cols = 5
        num_rows = (l + num_cols - 1) // num_cols
        fig, axes = plt.subplots(
            nrows=num_rows, ncols=num_cols,
            sharex=True, sharey=True, figsize=(20, 20))
        for idx, ax in enumerate(axes.flat, start=0):
            img = images[idx]
            masked_img = img.copy()
            masked_img[hand_masks[idx] == 1, ...] = (0, 255, 0)
            masked_img[obj_masks[idx] == 1, ...] = (255, 0, 255)
            img = cv2.addWeighted(img, 0.8, masked_img, 0.2, 1.0)
            img = odlib.draw_bboxes_image_array(
                img, hand_bbox_dicts[idx][side][None], color='red')
            odlib.draw_bboxes_image(img, obj_bboxes[idx][None], color='blue')
            img = np.asarray(img)
            ax.imshow(img)
            ax.set_axis_off()
            if idx == l-1:
                break

        plt.tight_layout()
        return fig

    def get_camera(self, index=-1) -> BatchCameraManager:
        global_cam = CameraManager(
            # fx=1050, fy=1050, cx=960, cy=540,
            fx=1050, fy=1050, cx=1280, cy=0,
            img_h=1080, img_w=1920)
        new_w, new_h = self.image_size
        global_cam = global_cam.resize(new_h=new_h, new_w=new_w)
        info = self.data_infos[index]
        bsize = info.end - info.start + 1
        if self.sample_frames > 0:
            bsize = min(self.sample_frames, bsize)
        batch_global_cam = global_cam.repeat(bsize, device='cpu')
        return batch_global_cam

    def __getitem__(self, index):
        """
        Returns:
            images: ndarray (N, H, W, 3) RGB
                note frankmocap requires `BGR` input
            hand_bbox_dicts: list of dict
                - left_hand/right_hand: ndarray (4,) of (x0, y0, w, h)
            obj_bbox_arrs: (N, 4) xywh
            object_masks: (N, H, W)
                - fg: 1, ignore -1, bg 0
            hand_masks: (N, H, W)
            cat: str, object categroy
        """
        info = self.data_infos[index]
        vid, cat, side, start, end = \
            info.vid, info.cat, info.side, info.start, info.end
        if self.sample_frames < 0 and end - start > 100:
            raise NotImplementedError(f"frames more than 100 : {end - start}.")
        images = []
        hand_bbox_dicts = []
        obj_bbox_arrs = []
        object_masks = []
        hand_masks = []
        if self.sample_frames < 0 or (end-start+1 < self.sample_frames):
            frames = range(start, end+1)
        else:
            frames = np.linspace(start, end, num=self.sample_frames, dtype=int)

        for frame_idx in frames:
            image = read_epic_image(
                vid, frame_idx, as_pil=True)
            image = image.resize(self.image_size)
            image = np.asarray(image)

            # bboxes
            hand_box = self._get_hand_box(vid, frame_idx, side)
            if side == 'right':
                hand_bbox_dict = dict(right_hand=hand_box, left_hand=None)
            elif side == 'left':
                hand_bbox_dict = dict(right_hand=None, left_hand=hand_box)
            else:
                raise ValueError(f"Unknown side {side}.")

            obj_bbox_arr = self._get_obj_box(vid, frame_idx, cat)

            # masks
            path = f'{self.mask_dir}/{vid}/frame_{frame_idx:010d}.png'
            mask_hand, mask_obj = read_mask_with_occlusion(
                path,
                out_size=self.image_size, side=side, cat=cat,
                crop_hand_mask=self.crop_hand_mask,
                crop_hand_expand=HAND_MASK_KEEP_EXPAND,
                hand_box=self._get_hand_box(vid, frame_idx, side, expand=False))

            images.append(image)
            hand_bbox_dicts.append(hand_bbox_dict)
            obj_bbox_arrs.append(obj_bbox_arr)
            hand_masks.append(mask_hand)
            object_masks.append(mask_obj)

        side_return = f"{side}_hand"
        images = np.stack(images)
        obj_bbox_arrs = torch.as_tensor(obj_bbox_arrs)
        hand_masks = torch.as_tensor(hand_masks)
        object_masks = torch.as_tensor(object_masks)

        element = DataElement(
            images=images,
            hand_bbox_dicts=hand_bbox_dicts,
            side_return=side_return,
            obj_bboxes=obj_bbox_arrs,
            hand_masks=hand_masks,
            object_masks=object_masks,
            cat=cat
        )
        return element


if __name__ == '__main__':
    dataset = EpicClipDataset(
        image_sets='/home/skynet/Zhifan/data/epic_analysis/gt_clips.json')
    item = (dataset[0])
    print(item)
