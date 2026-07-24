#!/usr/bin/env python3
"""Quantify path-to-obstacle clearance: stock config vs clearance-fix config.

Emulates GlobalPlanner._find_wide_path exactly (try sizes wide-to-narrow,
first that yields a path wins) and measures the minimum distance from the
planned path centerline to the obstacle bounding box.

Go2 geometry: 0.70m long x 0.31m wide -> body edge sweeps up to ~0.35m from
centerline. Any clearance below that is a potential graze.
"""
import numpy as np
import open3d as o3d

from dimos.core.global_config import GlobalConfig
from dimos.mapping.occupancy.path_map import make_navigation_map
from dimos.mapping.pointclouds.occupancy import OCCUPANCY_ALGOS
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.navigation.replanning_a_star.min_cost_astar import min_cost_astar

START = (0.0, 0.0)
GOAL = (2.5, 0.0)


def cloud_from(points):
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(np.asarray(points, float))
    return PointCloud2(pointcloud=pc, ts=1.0, frame_id="world")


def floor_points(x0=-1.0, x1=4.5, y0=-2.5, y1=2.5, step=0.04):
    xs = np.arange(x0, x1, step)
    ys = np.arange(y0, y1, step)
    g = np.stack(np.meshgrid(xs, ys), -1).reshape(-1, 2)
    return np.column_stack([g, np.zeros(len(g))])


def box(cx, cy, w, d, h, step=0.03):
    pts = []
    xs = np.arange(cx - w / 2, cx + w / 2 + 1e-9, step)
    ys = np.arange(cy - d / 2, cy + d / 2 + 1e-9, step)
    zs = np.arange(0, h + 1e-9, step)
    for x in xs:
        for z in zs:
            pts += [[x, cy - d / 2, z], [x, cy + d / 2, z]]
    for y in ys:
        for z in zs:
            pts += [[cx - w / 2, y, z], [cx + w / 2, y, z]]
    for x in xs:
        for y in ys:
            pts.append([x, y, h])
    return np.array(pts)


SCENES = {
    # open world, fully observed floor — sanity baseline
    "open_box": (floor_points(), [box(1.25, 0.0, 0.4, 0.4, 0.5)], (1.05, 1.45, -0.2, 0.2)),
    # REALISTIC: only a 1m-wide corridor along the walk direction has been
    # scanned (rest is UNKNOWN, 80/cell for A*); box partially blocks it.
    # This reproduces the venue graze: cheapest route is the known sliver
    # right at the inflation boundary.
    "corridor_box": (
        floor_points(y0=-0.5, y1=0.5),
        [box(1.25, 0.0, 0.4, 0.4, 0.5)],
        (1.05, 1.45, -0.2, 0.2),
    ),
    # 1.2m fully-known gap between two boxes — fallback must still find a path
    "gap_1.2m": (
        floor_points(),
        [box(1.25, 0.9, 0.4, 0.6, 0.5), box(1.25, -0.9, 0.4, 0.6, 0.5)],
        None,
    ),
}


def rect_distance(px, py, bbox):
    x0, x1, y0, y1 = bbox
    dx = max(x0 - px, 0.0, px - x1)
    dy = max(y0 - py, 0.0, py - y1)
    return float(np.hypot(dx, dy))


def find_wide_path(nav_binary_cm, gc, sizes):
    """Emulate GlobalPlanner._find_wide_path (voronoi far-map variant)."""
    for size in sizes:
        nav = make_navigation_map(nav_binary_cm, gc.robot_width * size, "simple", "voronoi")
        path = min_cost_astar(nav, GOAL, START)
        if path and path.poses:
            return size, path
    return None, None


def run(label, gc, sizes):
    print(f"\n== {label}: robot_width={gc.robot_width} sizes={sizes}")
    for name, (floor, obstacles, bbox) in SCENES.items():
        pts = np.vstack([floor] + list(obstacles))
        cm = OCCUPANCY_ALGOS["simple"](cloud_from(pts), min_height=0.15, max_height=2.0)
        size, path = find_wide_path(cm, gc, sizes)
        if path is None:
            print(f"  {name:<14} NO PATH")
            continue
        if bbox is None:  # gap scene: measure to both boxes
            bbs = [(1.05, 1.45, 0.6, 1.2), (1.05, 1.45, -1.2, -0.6)]
        else:
            bbs = [bbox]
        clearance = min(
            rect_distance(p.position.x, p.position.y, bb)
            for p in path.poses for bb in bbs
        )
        graze = "GRAZE RISK" if clearance < 0.35 else "ok"
        print(f"  {name:<14} size={size}x  min_clearance={clearance:.2f}m  [{graze}]")


stock = GlobalConfig()
fixed = GlobalConfig(robot_width=0.5)

run("STOCK (current)", stock, [1.1])
run("FIXED", fixed, [2.2, 1.7, 1.3, 1.1])
