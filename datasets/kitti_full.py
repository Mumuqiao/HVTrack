import os.path as osp
import pandas as pd
import pickle as pkl
import numpy as np
from pyquaternion import Quaternion
from tqdm import tqdm
import bisect
import torch

from .utils import *
from .base_dataset import BaseDataset, EvalDatasetWrapper
from utils import pl_ddp_rank


class KITTIFull(BaseDataset):

    def __init__(self, split_type, cfg, log):
        super().__init__(split_type, cfg, log)

        assert cfg.category_name in [
            'Van', 'Car', 'Pedestrian', 'Cyclist', 'All']

        if not cfg.debug:
            split_type_to_scene_ids = dict(
                train=list(range(0, 17)),
                val=list(range(17, 19)),
                test=list(range(19, 21))
            )
        else:
            split_type_to_scene_ids = dict(
                train=[0],
                val=[18],
                test=[19]
            )
        self.preload_interval = getattr(cfg,'preload_interval', 1)
        self.preload_offset = cfg.preload_offset if split_type == 'train' else -1
        self.cache = cfg.cache_train if split_type == 'train' else cfg.cache_eval
        self.calibration_info = {}

        scene_ids = split_type_to_scene_ids[split_type]
        self.tracklet_annotations = self._build_tracklet_annotations(scene_ids)

        self.tracklet_num_frames = [len(tracklet_anno)
                                    for tracklet_anno in self.tracklet_annotations]
        self.tracklet_st_frame_id = []
        self.tracklet_ed_frame_id = []
        last_ed_frame_id = 0
        for num_frames in self.tracklet_num_frames:
            assert num_frames > 0
            self.tracklet_st_frame_id.append(last_ed_frame_id)
            last_ed_frame_id += num_frames
            self.tracklet_ed_frame_id.append(last_ed_frame_id)

        if self.cache:
            if not cfg.debug:
                cache_file_dir = osp.join(
                    self.cfg.data_root_dir, f'KITTI_{self.cfg.category_name}_{split_type}_{self.cfg.coordinate_mode}_{self.preload_offset}_{self.preload_interval}.cache')
            else:
                cache_file_dir = osp.join(
                    self.cfg.data_root_dir, f'KITTI_DEBUG_{self.cfg.category_name}_{split_type}_{self.cfg.coordinate_mode}_{self.preload_offset}_{self.preload_interval}.cache')
            if osp.exists(cache_file_dir):
                self.log.info(f'Loading data from cache file {cache_file_dir}')
                with open(cache_file_dir, 'rb') as f:
                    tracklets = pkl.load(f)
            else:
                tracklets = []
                for tracklet_id in tqdm(range(len(self.tracklet_annotations)), desc='[%6s]Loading pcds ' % self.split_type.upper(), disable=pl_ddp_rank() != 0):
                    frames = []
                    for frame_anno in self.tracklet_annotations[tracklet_id]:
                        frames.append(self._build_frame(frame_anno))

                    comp_template_pcd = merge_template_pcds(
                        [frame['pcd'] for frame in frames],
                        [frame['bbox'] for frame in frames],
                        offset=cfg.model_offset,
                        scale=cfg.model_scale
                    )
                    if self.preload_offset > 0:
                        for frame in frames:
                            frame['pcd'] = crop_pcd_axis_aligned(
                                frame['pcd'], frame['bbox'], offset=self.preload_offset)

                    tracklets.append({
                        'comp_template_pcd': comp_template_pcd,
                        'frames': frames
                    })

                with open(cache_file_dir, 'wb') as f:
                    self.log.info(
                        f'Saving data to cache file {cache_file_dir}')
                    pkl.dump(tracklets, f)
            self.tracklets = tracklets
        else:
            self.tracklets = None

    def get_dataset(self):
        if self.split_type == 'train':
            return TrainDatasetWrapper(self, self.cfg, self.log)
        else:
            return EvalDatasetWrapper(self, self.cfg, self.log)

    def num_tracklets(self):
        return len(self.tracklet_annotations)

    def num_frames(self):
        return self.tracklet_ed_frame_id[-1]

    def num_tracklet_frames(self, tracklet_id):
        return self.tracklet_num_frames[tracklet_id]

    def get_frame(self, tracklet_id, frame_id):
        if self.tracklets:
            frame = self.tracklets[tracklet_id]['frames'][frame_id]
            return frame
        else:
            frame_anno = self.tracklet_annotations[tracklet_id][frame_id]
            frame = self._build_frame(frame_anno)
            if self.preload_offset > 0:
                frame['pcd'] = crop_pcd_axis_aligned(
                    frame['pcd'], frame['bbox'], offset=self.preload_offset)
            return frame

    def get_comp_template_pcd(self, tracklet_id):
        comp_template_pcd = self.tracklets[tracklet_id]['comp_template_pcd']
        return comp_template_pcd

    def get_tracklet_frame_id(self, idx):
        tracklet_id = bisect.bisect_right(
            self.tracklet_ed_frame_id, idx)
        assert self.tracklet_st_frame_id[
            tracklet_id] <= idx and idx < self.tracklet_ed_frame_id[tracklet_id]
        frame_id = idx - \
            self.tracklet_st_frame_id[tracklet_id]
        return tracklet_id, frame_id

    def _build_tracklet_annotations(self, scene_ids):
        tracklet_annotations = []
        for scene_id in tqdm(scene_ids, desc='[%6s]Loading annos' % self.split_type.upper(), disable=pl_ddp_rank() != 0):
            annotation_file_dir = osp.join(
                self.cfg.data_root_dir, 'label_02/%04d.txt' % scene_id)
            data = pd.read_csv(
                annotation_file_dir,
                sep=' ',
                names=[
                    'frame', 'track_id', 'type', 'truncated', 'occluded',
                    'alpha', 'bbox_left', 'bbox_top', 'bbox_right', 'bbox_bottom',
                    'height', 'width', 'length', 'x', 'y', 'z', 'rotation_y'
                ]
            )

            if self.cfg.category_name == 'All':
                data = data[(data["type"] == 'Car') |
                            (data["type"] == 'Van') |
                            (data["type"] == 'Pedestrian') |
                            (data["type"] == 'Cyclist')]
            else:
                data = data[data["type"] == self.cfg.category_name]

            data.insert(loc=0, column='scene', value=scene_id)
            track_ids = sorted(data.track_id.unique())
            for track_id in track_ids:
                tracklet_anno = data[data['track_id'] == track_id]
                tracklet_anno = tracklet_anno.sort_values(by=['frame'])
                tracklet_anno = [anno for index, anno in tracklet_anno.iterrows()]
                if self.preload_interval == 'all':
                    if self.split_type == 'train':
                        sample_intervals = [1,2,3,5,10]
                        for interval in sample_intervals:
                            for i in range(min(len(tracklet_anno),interval)):
                                temp_tracklet_anno = tracklet_anno[i::interval]
                                tracklet_annotations.append(temp_tracklet_anno)
                    else:
                        interval = 5
                        for i in range(min(len(tracklet_anno),interval)):
                            temp_tracklet_anno = tracklet_anno[i::interval]
                            tracklet_annotations.append(temp_tracklet_anno)
                    # else:
                    #     AssertionError
                else:
                    for i in range(min(len(tracklet_anno),self.preload_interval)):
                        temp_tracklet_anno = tracklet_anno[i::self.preload_interval]
                        tracklet_annotations.append(temp_tracklet_anno)
        return tracklet_annotations

    @staticmethod
    def _read_calibration_file(filepath):
        """Read in a calibration file and parse into a dictionary."""
        data = {}
        with open(filepath, 'r') as f:
            for line in f.readlines():
                values = line.split()
                # The only non-float values in these files are dates, which
                # we don't care about anyway
                try:
                    data[values[0]] = np.array(
                        [float(x) for x in values[1:]]).reshape(3, 4)
                except ValueError:
                    pass
        return data

    def _build_frame(self, frame_anno):
        scene_id = frame_anno['scene']
        frame_id = frame_anno['frame']
        if scene_id in self.calibration_info:
            calib = self.calibration_info[scene_id]
        else:
            calib = self._read_calibration_file(
                osp.join(self.cfg.data_root_dir, 'calib/%04d.txt' % scene_id))
            self.calibration_info[scene_id] = calib
        velo_to_cam = np.vstack((calib["Tr_velo_cam"], np.array([0, 0, 0, 1])))

        assert self.cfg.coordinate_mode in ['camera', 'velodyne']

        if self.cfg.coordinate_mode == 'camera':
            bbox_center = [frame_anno["x"], frame_anno["y"] -
                           frame_anno["height"] / 2, frame_anno["z"]]
            size = [frame_anno["width"],
                    frame_anno["length"], frame_anno["height"]]
            orientation = Quaternion(
                axis=[0, 1, 0], radians=frame_anno["rotation_y"]) * Quaternion(
                axis=[1, 0, 0], radians=np.pi / 2)
            bbox = BoundingBox(bbox_center, size, orientation)
        else:
            box_center_cam = np.array(
                [frame_anno["x"], frame_anno["y"] - frame_anno["height"] / 2, frame_anno["z"], 1])
            # transform bb from camera coordinate into velo coordinates
            box_center_velo = np.dot(
                np.linalg.inv(velo_to_cam), box_center_cam)
            box_center_velo = box_center_velo[:3]
            size = [frame_anno["width"],
                    frame_anno["length"], frame_anno["height"]]
            orientation = Quaternion(
                axis=[0, 0, -1], radians=frame_anno["rotation_y"]) * Quaternion(axis=[0, 0, -1], degrees=90)
            bbox = BoundingBox(box_center_velo, size, orientation)
        try:
            pcd_file_dir = osp.join(
                self.cfg.data_root_dir, 'velodyne/%04d' % scene_id, '%06d.bin' % frame_id)
            pcd = PointCloud(np.fromfile(
                pcd_file_dir, dtype=np.float32).reshape(-1, 4).T)
            if self.cfg.coordinate_mode == 'camera':
                pcd.transform(velo_to_cam)
        except:
            # in case the Point cloud is missing
            # (0001/[000177-000180].bin)
            pcd = PointCloud(np.array([[0, 0, 0]]).T)

        return {'pcd': pcd, 'bbox': bbox, 'anno': frame_anno}


def print_np(**kwargs):
    for k, v in kwargs.items():
        print(k, np.concatenate((v[:5], v[-5:]), axis=0))
