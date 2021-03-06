# Modified from https://github.com/sxyu/pixel-nerf/blob/master/src/data/DVRDataset.py

from dataclasses import replace
import os

import torch
import torch.nn.functional as F
import glob
import imageio
import numpy as np
import cv2
from torchvision import transforms

from datasets import register


def get_image_to_tensor_balanced(image_size=0):
    ops = []
    if image_size > 0:
        ops.append(transforms.Resize(image_size))
    ops.extend(
        # [transforms.ToTensor(), transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),]
        [transforms.ToTensor()]
    )
    return transforms.Compose(ops)


def get_mask_to_tensor():
    return transforms.Compose(
        [transforms.ToTensor(), transforms.Normalize((0.0,), (1.0,))]
    )


import torchvision.transforms.functional_tensor as F_t


def color_jitter_augment(images):
    hue_range = 0.1
    saturation_range = 0.1
    brightness_range = 0.1
    contrast_range = 0.1

    hue_range = [-hue_range, hue_range]
    saturation_range = [1 - saturation_range, 1 + saturation_range]
    brightness_range = [1 - brightness_range, 1 + brightness_range]
    contrast_range = [1 - contrast_range, 1 + contrast_range]

    hue_factor = np.random.uniform(*hue_range)
    saturation_factor = np.random.uniform(*saturation_range)
    brightness_factor = np.random.uniform(*brightness_range)
    contrast_factor = np.random.uniform(*contrast_range)
    for i in range(len(images)):
        tmp = images[i]
        tmp = F_t.adjust_saturation(tmp, saturation_factor)
        tmp = F_t.adjust_hue(tmp, hue_factor)
        tmp = F_t.adjust_contrast(tmp, contrast_factor)
        tmp = F_t.adjust_brightness(tmp, brightness_factor)
        images[i] = tmp
    return images


@register('pixelnerf_dvr')
class PixelnerfDvr(torch.utils.data.Dataset):
    """
    Dataset from DVR (Niemeyer et al. 2020)
    Provides 3D-R2N2 and NMR renderings
    """

    def __init__(
        self, sub_format, root_path, split, n_support, n_query, support_lst=None, repeat=1, viewrng=None, retcat=False,
    ):
        """
        :param path dataset root path, contains metadata.yml
        :param stage train | val | test
        :param list_prefix prefix for split lists: <list_prefix>[train, val, test].lst
        :param image_size result image size (resizes if different); None to keep original size
        :param sub_format shapenet | dtu dataset sub-type.
        :param scale_focal if true, assume focal length is specified for
        image of side length 2 instead of actual image size. This is used
        where image coordinates are placed in [-1, 1].
        """
        list_prefix = "softras_"
        image_size = None
        max_imgs = 100000
        scale_focal = True
        z_near = 1.2
        z_far = 4.0

        if sub_format == 'dtu':
            list_prefix = "new_"
            self.split = split
            training = (split != 'test') ##
            if training:
                max_imgs = 49
            scale_focal = False
            z_near = 0.1
            z_far = 5.0

        super().__init__()
        self.base_path = root_path
        assert os.path.exists(self.base_path)

        cats = sorted([x for x in glob.glob(os.path.join(root_path, "*")) if os.path.isdir(x)])
        b_cats = sorted([os.path.basename(c) for c in cats])
        cat2int = {c: i for i, c in enumerate(b_cats)}
        self.cats = b_cats
        self.cat2int = cat2int
        self.retcat = retcat

        if split == "train":
            file_lists = [os.path.join(x, list_prefix + "train.lst") for x in cats]
        elif split == "val":
            file_lists = [os.path.join(x, list_prefix + "val.lst") for x in cats]
        elif split == "test":
            file_lists = [os.path.join(x, list_prefix + "test.lst") for x in cats]

        all_objs = []
        for file_list in file_lists:
            if not os.path.exists(file_list):
                continue
            base_dir = os.path.dirname(file_list)
            cat = os.path.basename(base_dir)
            with open(file_list, "r") as f:
                objs = [(cat, os.path.join(base_dir, x.strip())) for x in f.readlines()]
            all_objs.extend(objs)

        self.all_objs = all_objs
        self.stage = split

        self.image_to_tensor = get_image_to_tensor_balanced()
        self.mask_to_tensor = get_mask_to_tensor()
        print(
            "Loading DVR dataset",
            self.base_path,
            "stage",
            split,
            len(self.all_objs),
            "objs",
            "type:",
            sub_format,
        )

        self.image_size = image_size
        if sub_format == "dtu":
            self._coord_trans_world = torch.tensor(
                [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]],
                dtype=torch.float32,
            )
            self._coord_trans_cam = torch.tensor(
                [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]],
                dtype=torch.float32,
            )
        else:
            self._coord_trans_world = torch.tensor(
                [[1, 0, 0, 0], [0, 0, -1, 0], [0, 1, 0, 0], [0, 0, 0, 1]],
                dtype=torch.float32,
            )
            self._coord_trans_cam = torch.tensor(
                [[1, 0, 0, 0], [0, -1, 0, 0], [0, 0, -1, 0], [0, 0, 0, 1]],
                dtype=torch.float32,
            )
        self.sub_format = sub_format
        self.scale_focal = scale_focal
        self.max_imgs = max_imgs

        self.z_near = z_near
        self.z_far = z_far
        self.lindisp = False

        self.n_support = n_support
        self.n_query = n_query
        self.support_lst = support_lst
        self.repeat = repeat
        self.viewrng = viewrng

    def __len__(self):
        return len(self.all_objs) * self.repeat

    def __getitem__(self, index):
        index %= len(self.all_objs)
        cat, root_dir = self.all_objs[index]

        rgb_paths = [
            x
            for x in glob.glob(os.path.join(root_dir, "image", "*"))
            if (x.endswith(".jpg") or x.endswith(".png"))
        ]
        rgb_paths = sorted(rgb_paths)
        mask_paths = sorted(glob.glob(os.path.join(root_dir, "mask", "*.png")))
        if len(mask_paths) == 0:
            mask_paths = [None] * len(rgb_paths)

        if len(rgb_paths) <= self.max_imgs:
            sel_indices = np.arange(len(rgb_paths))
        else:
            sel_indices = np.random.choice(len(rgb_paths), self.max_imgs, replace=False)
            # rgb_paths = [rgb_paths[i] for i in sel_indices]
            # mask_paths = [mask_paths[i] for i in sel_indices]

        if self.viewrng is not None:
            l, r = self.viewrng
            sel_indices = sel_indices[l: r]

        if self.support_lst is None:
            sel_indices = np.random.choice(sel_indices, self.n_support + self.n_query, replace=False)
        else:
            rest_inds = []
            for i in sel_indices:
                if i not in self.support_lst:
                    rest_inds.append(i)
            rest_inds = np.random.choice(rest_inds, self.n_query, replace=False).tolist()
            sel_indices = self.support_lst + rest_inds

        rgb_paths = [rgb_paths[i] for i in sel_indices]
        mask_paths = [mask_paths[i] for i in sel_indices]
        _len_rgb_paths = len(sel_indices)

        cam_path = os.path.join(root_dir, "cameras.npz")
        all_cam = np.load(cam_path)

        all_imgs = []
        all_poses = []
        all_masks = []
        all_bboxes = []
        focal = None
        if self.sub_format != "shapenet":
            # Prepare to average intrinsics over images
            fx, fy, cx, cy = 0.0, 0.0, 0.0, 0.0

        for idx, (rgb_path, mask_path) in enumerate(zip(rgb_paths, mask_paths)):
            i = sel_indices[idx]
            img = imageio.imread(rgb_path)[..., :3]
            if self.scale_focal:
                x_scale = img.shape[1] / 2.0
                y_scale = img.shape[0] / 2.0
                xy_delta = 1.0
            else:
                x_scale = y_scale = 1.0
                xy_delta = 0.0

            if mask_path is not None:
                mask = imageio.imread(mask_path)
                if len(mask.shape) == 2:
                    mask = mask[..., None]
                mask = mask[..., :1]
            if self.sub_format == "dtu":
                # Decompose projection matrix
                # DVR uses slightly different format for DTU set
                P = all_cam["world_mat_" + str(i)]
                P = P[:3]

                K, R, t = cv2.decomposeProjectionMatrix(P)[:3]
                K = K / K[2, 2]

                pose = np.eye(4, dtype=np.float32)
                pose[:3, :3] = R.transpose()
                pose[:3, 3] = (t[:3] / t[3])[:, 0]

                scale_mtx = all_cam.get("scale_mat_" + str(i))
                if scale_mtx is not None:
                    norm_trans = scale_mtx[:3, 3:]
                    norm_scale = np.diagonal(scale_mtx[:3, :3])[..., None]

                    pose[:3, 3:] -= norm_trans
                    pose[:3, 3:] /= norm_scale

                fx += torch.tensor(K[0, 0]) * x_scale
                fy += torch.tensor(K[1, 1]) * y_scale
                cx += (torch.tensor(K[0, 2]) + xy_delta) * x_scale
                cy += (torch.tensor(K[1, 2]) + xy_delta) * y_scale
            else:
                # ShapeNet
                wmat_inv_key = "world_mat_inv_" + str(i)
                wmat_key = "world_mat_" + str(i)
                if wmat_inv_key in all_cam:
                    extr_inv_mtx = all_cam[wmat_inv_key]
                else:
                    extr_inv_mtx = all_cam[wmat_key]
                    if extr_inv_mtx.shape[0] == 3:
                        extr_inv_mtx = np.vstack((extr_inv_mtx, np.array([0, 0, 0, 1])))
                    extr_inv_mtx = np.linalg.inv(extr_inv_mtx)

                intr_mtx = all_cam["camera_mat_" + str(i)]
                fx, fy = intr_mtx[0, 0], intr_mtx[1, 1]
                assert abs(fx - fy) < 1e-9
                fx = fx * x_scale
                if focal is None:
                    focal = torch.tensor((fx, fx), dtype=torch.float32)
                else:
                    assert abs(fx - focal[0]) < 1e-5
                pose = extr_inv_mtx

            pose = (
                self._coord_trans_world
                @ torch.tensor(pose, dtype=torch.float32)
                @ self._coord_trans_cam
            )

            img_tensor = self.image_to_tensor(img)
            if mask_path is not None:
                mask_tensor = self.mask_to_tensor(mask)

                rows = np.any(mask, axis=1)
                cols = np.any(mask, axis=0)
                rnz = np.where(rows)[0]
                cnz = np.where(cols)[0]
                if len(rnz) == 0:
                    raise RuntimeError(
                        "ERROR: Bad image at", rgb_path, "please investigate!"
                    )
                rmin, rmax = rnz[[0, -1]]
                cmin, cmax = cnz[[0, -1]]
                bbox = torch.tensor([cmin, rmin, cmax, rmax], dtype=torch.float32)
                all_masks.append(mask_tensor)
                all_bboxes.append(bbox)

            all_imgs.append(img_tensor)
            all_poses.append(pose)

        if self.sub_format != "shapenet":
            fx /= _len_rgb_paths
            fy /= _len_rgb_paths
            cx /= _len_rgb_paths
            cy /= _len_rgb_paths
            focal = torch.tensor((fx, fy), dtype=torch.float32)
            c = torch.tensor((cx, cy), dtype=torch.float32)
            all_bboxes = None
        elif mask_path is not None:
            all_bboxes = torch.stack(all_bboxes)

        all_imgs = torch.stack(all_imgs)
        all_poses = torch.stack(all_poses)
        if len(all_masks) > 0:
            all_masks = torch.stack(all_masks)
        else:
            all_masks = None

        if self.image_size is not None and all_imgs.shape[-2:] != self.image_size:
            scale = self.image_size[0] / all_imgs.shape[-2]
            focal *= scale
            if self.sub_format != "shapenet":
                c *= scale
            elif mask_path is not None:
                all_bboxes *= scale

            all_imgs = F.interpolate(all_imgs, size=self.image_size, mode="area")
            if all_masks is not None:
                all_masks = F.interpolate(all_masks, size=self.image_size, mode="area")

        # result = {
        #     "path": root_dir,
        #     "img_id": index,
        #     "focal": focal,
        #     "images": all_imgs,
        #     "poses": all_poses,
        # }
        # if all_masks is not None:
        #     result["masks"] = all_masks
        # if self.sub_format != "shapenet":
        #     result["c"] = c
        # else:
        #     result["bbox"] = all_bboxes

        if self.sub_format == 'dtu' and self.split == 'train':
            all_imgs = color_jitter_augment(all_imgs)

        t = self.n_support
        all_poses = all_poses[:, :3, :4]
        focal = focal.repeat(len(all_poses), 1)
        result = {
            'support_imgs': all_imgs[:t],
            'support_poses': all_poses[:t],
            'support_focals': focal[:t],
            'query_imgs': all_imgs[t:],
            'query_poses': all_poses[t:],
            'query_focals': focal[t:],
            'near': self.z_near,
            'far': self.z_far,
        }
        if self.retcat:
            result['cat'] = self.cat2int[cat]
        return result

    def select(self, index, si, qi):
        index %= len(self.all_objs)
        cat, root_dir = self.all_objs[index]

        rgb_paths = [
            x
            for x in glob.glob(os.path.join(root_dir, "image", "*"))
            if (x.endswith(".jpg") or x.endswith(".png"))
        ]
        rgb_paths = sorted(rgb_paths)
        mask_paths = sorted(glob.glob(os.path.join(root_dir, "mask", "*.png")))
        if len(mask_paths) == 0:
            mask_paths = [None] * len(rgb_paths)

        if len(rgb_paths) <= self.max_imgs:
            sel_indices = np.arange(len(rgb_paths))
        else:
            assert False

        _len_rgb_paths = len(sel_indices)

        if self.viewrng is not None:
            assert False

        sel_indices = [si, qi]

        rgb_paths = [rgb_paths[i] for i in sel_indices]
        mask_paths = [mask_paths[i] for i in sel_indices]

        cam_path = os.path.join(root_dir, "cameras.npz")
        all_cam = np.load(cam_path)

        all_imgs = []
        all_poses = []
        all_masks = []
        all_bboxes = []
        focal = None
        if self.sub_format != "shapenet":
            # Prepare to average intrinsics over images
            fx, fy, cx, cy = 0.0, 0.0, 0.0, 0.0

        for idx, (rgb_path, mask_path) in enumerate(zip(rgb_paths, mask_paths)):
            i = sel_indices[idx]
            img = imageio.imread(rgb_path)[..., :3]
            if self.scale_focal:
                x_scale = img.shape[1] / 2.0
                y_scale = img.shape[0] / 2.0
                xy_delta = 1.0
            else:
                x_scale = y_scale = 1.0
                xy_delta = 0.0

            if mask_path is not None:
                mask = imageio.imread(mask_path)
                if len(mask.shape) == 2:
                    mask = mask[..., None]
                mask = mask[..., :1]
            if self.sub_format == "dtu":
                # Decompose projection matrix
                # DVR uses slightly different format for DTU set
                P = all_cam["world_mat_" + str(i)]
                P = P[:3]

                K, R, t = cv2.decomposeProjectionMatrix(P)[:3]
                K = K / K[2, 2]

                pose = np.eye(4, dtype=np.float32)
                pose[:3, :3] = R.transpose()
                pose[:3, 3] = (t[:3] / t[3])[:, 0]

                scale_mtx = all_cam.get("scale_mat_" + str(i))
                if scale_mtx is not None:
                    norm_trans = scale_mtx[:3, 3:]
                    norm_scale = np.diagonal(scale_mtx[:3, :3])[..., None]

                    pose[:3, 3:] -= norm_trans
                    pose[:3, 3:] /= norm_scale

                fx += torch.tensor(K[0, 0]) * x_scale
                fy += torch.tensor(K[1, 1]) * y_scale
                cx += (torch.tensor(K[0, 2]) + xy_delta) * x_scale
                cy += (torch.tensor(K[1, 2]) + xy_delta) * y_scale
            else:
                # ShapeNet
                wmat_inv_key = "world_mat_inv_" + str(i)
                wmat_key = "world_mat_" + str(i)
                if wmat_inv_key in all_cam:
                    extr_inv_mtx = all_cam[wmat_inv_key]
                else:
                    extr_inv_mtx = all_cam[wmat_key]
                    if extr_inv_mtx.shape[0] == 3:
                        extr_inv_mtx = np.vstack((extr_inv_mtx, np.array([0, 0, 0, 1])))
                    extr_inv_mtx = np.linalg.inv(extr_inv_mtx)

                intr_mtx = all_cam["camera_mat_" + str(i)]
                fx, fy = intr_mtx[0, 0], intr_mtx[1, 1]
                assert abs(fx - fy) < 1e-9
                fx = fx * x_scale
                if focal is None:
                    focal = fx
                else:
                    assert abs(fx - focal) < 1e-5
                pose = extr_inv_mtx

            pose = (
                self._coord_trans_world
                @ torch.tensor(pose, dtype=torch.float32)
                @ self._coord_trans_cam
            )

            img_tensor = self.image_to_tensor(img)
            if mask_path is not None:
                mask_tensor = self.mask_to_tensor(mask)

                rows = np.any(mask, axis=1)
                cols = np.any(mask, axis=0)
                rnz = np.where(rows)[0]
                cnz = np.where(cols)[0]
                if len(rnz) == 0:
                    raise RuntimeError(
                        "ERROR: Bad image at", rgb_path, "please investigate!"
                    )
                rmin, rmax = rnz[[0, -1]]
                cmin, cmax = cnz[[0, -1]]
                bbox = torch.tensor([cmin, rmin, cmax, rmax], dtype=torch.float32)
                all_masks.append(mask_tensor)
                all_bboxes.append(bbox)

            all_imgs.append(img_tensor)
            all_poses.append(pose)

        if self.sub_format != "shapenet":
            fx /= _len_rgb_paths
            fy /= _len_rgb_paths
            cx /= _len_rgb_paths
            cy /= _len_rgb_paths
            focal = torch.tensor((fx, fy), dtype=torch.float32)
            c = torch.tensor((cx, cy), dtype=torch.float32)
            all_bboxes = None
        elif mask_path is not None:
            all_bboxes = torch.stack(all_bboxes)

        all_imgs = torch.stack(all_imgs)
        all_poses = torch.stack(all_poses)
        if len(all_masks) > 0:
            all_masks = torch.stack(all_masks)
        else:
            all_masks = None

        if self.image_size is not None and all_imgs.shape[-2:] != self.image_size:
            scale = self.image_size[0] / all_imgs.shape[-2]
            focal *= scale
            if self.sub_format != "shapenet":
                c *= scale
            elif mask_path is not None:
                all_bboxes *= scale

            all_imgs = F.interpolate(all_imgs, size=self.image_size, mode="area")
            if all_masks is not None:
                all_masks = F.interpolate(all_masks, size=self.image_size, mode="area")

        t = self.n_support
        all_poses = all_poses[:, :3, :4]
        result = {
            'support_imgs': all_imgs[:t],
            'support_poses': all_poses[:t],
            'query_imgs': all_imgs[t:],
            'query_poses': all_poses[t:],
            'focal': focal,
            'near': self.z_near,
            'far': self.z_far,
        }
        if self.retcat:
            result['cat'] = self.cat2int[cat]
        return result
