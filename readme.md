# [ECCV24] 3D Single-object Tracking in Point Clouds with High Temporal Variation

**[[Paper]](https://arxiv.org/abs/2408.02049)**

This repository is the official release code for our ECCV24 paper HVTrack.

**We explore a new task in 3D SOT, and presented the first 3D SOT framework for high temporal variation scenarios, HVTrack.** Its three main components, RPM, BEA, and CPA, allow HVTrack to achieve robustness to point cloud variations, similar object distractions, and background noise. HVTrack significantly outperforms existing trackers in high temporal variation scenarios (11.3% and 15.7% improvement in success and precision at medium intensity of variation). **The performance gap between our HVTrack and existing trackers widens as variations are exacerbated.** It also surpasses existing methods in both nuScenes and Waymo benchmarks of regular tracking, achieving SOTA.

<p align="center">
<img src="https://cdn.jsdelivr.net/gh/Mumuqiao/pictures/Image/hvtrack.png" width="800"/>
</p>

<p align="center">
<img src="https://cdn.jsdelivr.net/gh/Mumuqiao/pictures/Image/HVTrack_thumbnail.png" width="400"/>
</p>


## Code issues
**I am busy on exploring the next stage of my career and don't have the time to organize all the code and data in the near future. I'll start by open-sourcing the most concerned codes like [Data Processing](./datasets/kitti_full.py), [BCA, and CPA](./modules/transformer_layer.py) to this repository.**

## Setup

* Please follow the instruction in [CXTrack](https://github.com/slothfulxtx/cxtrack3d) to build up dependencies.

