#
# Copyright (C) 2024, ShanghaiTech
# SVIP research group, https://github.com/svip-lab
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use 
# under the terms of the LICENSE.md file.
#
# For inquiries contact  huangbb@shanghaitech.edu.cn
#

import torch
import numpy as np
import os
import math
from tqdm import tqdm
from functools import partial
import open3d as o3d
import trimesh
from utils.render_utils import transform_poses_pca, focus_point_fn

def post_process_mesh(mesh, cluster_to_keep=1000):
    """
    Post-process a mesh to filter out floaters and disconnected parts
    """
    import copy
    print("post processing the mesh to have {} clusterscluster_to_kep".format(cluster_to_keep))
    mesh_0 = copy.deepcopy(mesh)
    with o3d.utility.VerbosityContextManager(o3d.utility.VerbosityLevel.Debug) as cm:
            triangle_clusters, cluster_n_triangles, cluster_area = (mesh_0.cluster_connected_triangles())

    triangle_clusters = np.asarray(triangle_clusters)
    cluster_n_triangles = np.asarray(cluster_n_triangles)
    cluster_area = np.asarray(cluster_area)
    n_cluster = np.sort(cluster_n_triangles.copy())[-cluster_to_keep]
    n_cluster = max(n_cluster, 50) # filter meshes smaller than 50
    triangles_to_remove = cluster_n_triangles[triangle_clusters] < n_cluster
    mesh_0.remove_triangles_by_mask(triangles_to_remove)
    mesh_0.remove_unreferenced_vertices()
    mesh_0.remove_degenerate_triangles()
    print("num vertices raw {}".format(len(mesh.vertices)))
    print("num vertices post {}".format(len(mesh_0.vertices)))
    return mesh_0

def to_cam_open3d(viewpoint_stack):
    camera_traj = []
    for i, viewpoint_cam in enumerate(viewpoint_stack):
        W = viewpoint_cam.image_width
        H = viewpoint_cam.image_height
        ndc2pix = torch.tensor([
            [W / 2, 0, 0, (W-1) / 2],
            [0, H / 2, 0, (H-1) / 2],
            [0, 0, 0, 1]]).float().cuda().T
        intrins =  (viewpoint_cam.projection_matrix @ ndc2pix)[:3,:3].T
        intrinsic=o3d.camera.PinholeCameraIntrinsic(
            width=viewpoint_cam.image_width,
            height=viewpoint_cam.image_height,
            cx = intrins[0,2].item(),
            cy = intrins[1,2].item(), 
            fx = intrins[0,0].item(), 
            fy = intrins[1,1].item()
        )

        extrinsic=np.asarray((viewpoint_cam.world_view_transform.T).cpu().numpy())
        camera = o3d.camera.PinholeCameraParameters()
        camera.extrinsic = extrinsic
        camera.intrinsic = intrinsic
        camera_traj.append(camera)

    return camera_traj


class GaussianExtractor(object):
    def __init__(self, gaussians, render, pipe, background):
        """
        a class that extracts attributes a scene presented by 2DGS

        Usage example:
        >>> gaussExtrator = GaussianExtractor(gaussians, render, pipe)
        >>> gaussExtrator.reconstruction(view_points)
        >>> mesh = gaussExtractor.export_mesh_bounded(...)
        """
        self.gaussians = gaussians
        self.render = partial(render, pipe=pipe, bg_color=background)
        self.gaussians = gaussians
        self.clean()

    @torch.no_grad()
    def clean(self):
        self.depthmaps = []
        self.rgbmaps = []
        self.viewpoint_stack = []

    @torch.no_grad()
    def reconstruction(self, viewpoint_stack):
        """
        reconstruct radiance field given cameras
        """
        self.clean()
        self.viewpoint_stack = viewpoint_stack
        for i, viewpoint_cam in tqdm(enumerate(self.viewpoint_stack), desc="reconstruct radiance fields"):
            render_pkg = self.render(viewpoint_cam, self.gaussians)
            rgb = render_pkg['render']
            alpha = render_pkg['render_alphas']
            normal = torch.nn.functional.normalize(render_pkg['render_normals'], dim=0)
            depth = render_pkg['render_depth']
            depth_normal = render_pkg['render_normals_from_depth']
            self.rgbmaps.append(rgb.cpu())
            self.depthmaps.append(depth.cpu())

        self.estimate_bounding_sphere()

    def estimate_bounding_sphere(self):
        """
        Estimate the bounding sphere given camera pose
        """
        torch.cuda.empty_cache()
        # check the street & aerial
        viewpoints = []
        for i, cam in enumerate(self.viewpoint_stack):
            if cam.image_type == 'aerial':
                viewpoints.append(cam)
            if cam.image_type == 'street':
                continue
        
        # c2ws = np.array([np.linalg.inv(np.asarray((cam.world_view_transform.T).cpu().numpy())) for cam in self.viewpoint_stack])
        c2ws = np.array([np.linalg.inv(np.asarray((cam.world_view_transform.T).cpu().numpy())) for cam in viewpoints])
        poses = c2ws[:,:3,:] @ np.diag([1, -1, -1, 1])
        center = (focus_point_fn(poses))
        self.radius = np.linalg.norm(c2ws[:,:3,3] - center, axis=-1).min()
        self.center = torch.from_numpy(center).float().cuda()
        print(f"The estimated bounding radius is {self.radius:.2f}")
        print(f"Use at least {2.0 * self.radius:.2f} for depth_trunc")

    @torch.no_grad()
    def extract_mesh_bounded(self, voxel_size=0.004, sdf_trunc=0.02, depth_trunc=3, mask_backgrond=True):
        """
        Perform TSDF fusion given a fixed depth range, used in the paper.
        
        voxel_size: the voxel size of the volume
        sdf_trunc: truncation value
        depth_trunc: maximum depth range, should depended on the scene's scales
        mask_backgrond: whether to mask backgroud, only works when the dataset have masks

        return o3d.mesh
        """
        print("Running tsdf volume integration ...")
        print(f'voxel_size: {voxel_size}')
        print(f'sdf_trunc: {sdf_trunc}')
        print(f'depth_truc: {depth_trunc}')

        volume = o3d.pipelines.integration.ScalableTSDFVolume(
            voxel_length= voxel_size,
            sdf_trunc=sdf_trunc,
            color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8
        )

        for i, cam_o3d in tqdm(enumerate(to_cam_open3d(self.viewpoint_stack)), desc="TSDF integration progress"):
            rgb = self.rgbmaps[i]
            depth = self.depthmaps[i]
            
            # if we have mask provided, use it
            if mask_backgrond and (self.viewpoint_stack[i].alpha_mask is not None):
                depth[(self.viewpoint_stack[i].alpha_mask < 0.5)] = 0

            # make open3d rgbd
            rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
                o3d.geometry.Image(np.asarray(np.clip(rgb.permute(1,2,0).cpu().numpy(), 0.0, 1.0) * 255, order="C", dtype=np.uint8)),
                o3d.geometry.Image(np.asarray(depth.permute(1,2,0).cpu().numpy(), order="C")),
                depth_trunc = depth_trunc, convert_rgb_to_intensity=False,
                depth_scale = 1.0
            )

            volume.integrate(rgbd, intrinsic=cam_o3d.intrinsic, extrinsic=cam_o3d.extrinsic)

        mesh = volume.extract_triangle_mesh()
        return mesh

    @torch.no_grad()
    def extract_mesh_unbounded(self, resolution=1024):
        """
        Experimental features, extracting meshes from unbounded scenes, not fully test across datasets. 
        return o3d.mesh
        """
        def contract(x):
            mag = torch.linalg.norm(x, ord=2, dim=-1)[..., None]
            return torch.where(mag < 1, x, (2 - (1 / mag)) * (x / mag))
        
        def uncontract(y):
            mag = torch.linalg.norm(y, ord=2, dim=-1)[..., None]
            return torch.where(mag < 1, y, (1 / (2-mag) * (y/mag)))

        def compute_sdf_perframe(i, points, depthmap, rgbmap, viewpoint_cam):
            """
                compute per frame sdf
            """
            new_points = torch.cat([points, torch.ones_like(points[...,:1])], dim=-1) @ viewpoint_cam.full_proj_transform
            z = new_points[..., -1:]
            pix_coords = (new_points[..., :2] / new_points[..., -1:])
            mask_proj = ((pix_coords > -1. ) & (pix_coords < 1.) & (z > 0)).all(dim=-1)
            sampled_depth = torch.nn.functional.grid_sample(depthmap.cuda()[None], pix_coords[None, None], mode='bilinear', padding_mode='border', align_corners=True).reshape(-1, 1)
            sampled_rgb = torch.nn.functional.grid_sample(rgbmap.cuda()[None], pix_coords[None, None], mode='bilinear', padding_mode='border', align_corners=True).reshape(3,-1).T
            sdf = (sampled_depth-z)
            return sdf, sampled_rgb, mask_proj