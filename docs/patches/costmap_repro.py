#!/usr/bin/env python3
"""Offline repro: which obstacle archetypes survive dimOS's default unitree-go2
costmap pipeline (CostMapper algo=height_cost) vs alternatives (simple/general).

Scene: flat floor at z=0 (the Go2 world frame puts ground ~z=0; odom z ~= 0.32
is body height). Robot start (0,0), goal (2,0), obstacle centered at x~1.0.

For each scene x algo:
  1. costmap  = OCCUPANCY_ALGOS[algo](cloud)          <- CostMapper._calculate_costmap
  2. stats    = max cost / lethal-cell count inside obstacle bbox
  3. navmap   = make_navigation_map(costmap, 0.33, "simple", "voronoi")  <- GlobalPlanner._find_wide_path
  4. path     = min_cost_astar(navmap, goal, start)   <- A* over inflated gradient map
  5. verdict  = does the path cut through the obstacle bbox (inflated by half robot width)
  6. clearance = PathClearance.is_obstacle_ahead() on a straight-line path (local planner safety net)
"""
import numpy as np
import open3d as o3d

from dimos.core.global_config import GlobalConfig
from dimos.mapping.occupancy.path_map import make_navigation_map
from dimos.mapping.pointclouds.occupancy import OCCUPANCY_ALGOS
from dimos.msgs.geometry_msgs.PoseStamped import PoseStamped
from dimos.msgs.geometry_msgs.Quaternion import Quaternion
from dimos.msgs.nav_msgs.Path import Path
from dimos.msgs.sensor_msgs.PointCloud2 import PointCloud2
from dimos.navigation.replanning_a_star.min_cost_astar import min_cost_astar
from dimos.navigation.replanning_a_star.path_clearance import PathClearance

START = (0.0, 0.0)
GOAL = (2.0, 0.0)


def cloud_from(points):
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(np.asarray(points, float))
    return PointCloud2(pointcloud=pc, ts=1.0, frame_id="world")


def floor_points(x0=-1.0, x1=4.0, y0=-2.0, y1=2.0, step=0.04):
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


def cylinder(cx, cy, r, h, step=0.03):
    pts = []
    for z in np.arange(0, h + 1e-9, step):
        for a in np.linspace(0, 2 * np.pi, 12, endpoint=False):
            pts.append([cx + r * np.cos(a), cy + r * np.sin(a), z])
    return np.array(pts)


SCENES = {
    # name: (points, bbox (x0,x1,y0,y1))
    "empty_floor": (np.zeros((0, 3)), (0.8, 1.2, -0.3, 0.3)),
    "low_box_h0.3": (box(1.0, 0.0, 0.5, 0.5, 0.3), (0.75, 1.25, -0.25, 0.25)),
    "mid_box_h0.5": (box(1.0, 0.0, 0.5, 0.5, 0.5), (0.75, 1.25, -0.25, 0.25)),
    "tall_box_h1.2": (box(1.0, 0.0, 0.5, 0.5, 1.2), (0.75, 1.25, -0.25, 0.25)),
    "wall_h1.5": (box(1.0, 0.0, 0.1, 2.0, 1.5), (0.9, 1.1, -1.0, 1.0)),
    "chair_legs_h0.45": (
        np.vstack([
            cylinder(0.85, -0.15, 0.02, 0.45), cylinder(0.85, 0.15, 0.02, 0.45),
            cylinder(1.15, -0.15, 0.02, 0.45), cylinder(1.15, 0.15, 0.02, 0.45),
        ]),
        (0.8, 1.2, -0.2, 0.2),
    ),
    "person_legs_h1.0": (
        np.vstack([cylinder(0.95, -0.08, 0.06, 1.0), cylinder(0.95, 0.08, 0.06, 1.0)]),
        (0.85, 1.05, -0.18, 0.18),
    ),
}


def bbox_cells(grid_msg, bbox):
    x0, x1, y0, y1 = bbox
    a = grid_msg.world_to_grid((x0, y0))
    b = grid_msg.world_to_grid((x1, y1))
    gx0, gx1 = sorted((int(a.x), int(b.x)))
    gy0, gy1 = sorted((int(a.y), int(b.y)))
    gx0 = max(gx0, 0); gy0 = max(gy0, 0)
    gx1 = min(gx1, grid_msg.width - 1); gy1 = min(gy1, grid_msg.height - 1)
    return grid_msg.grid[gy0:gy1 + 1, gx0:gx1 + 1]


def path_hits_bbox(path, bbox, margin=0.15):
    x0, x1, y0, y1 = bbox
    for p in path.poses:
        if (x0 - margin) <= p.position.x <= (x1 + margin) and \
           (y0 - margin) <= p.position.y <= (y1 + margin):
            return True
    return False


def straight_path():
    poses = []
    for x in np.arange(0.0, 2.01, 0.05):
        poses.append(PoseStamped(frame_id="world",
                                 position=[float(x), 0.0, 0.0],
                                 orientation=Quaternion(0, 0, 0, 1)))
    return Path(frame_id="world", poses=poses)


def main():
    gc = GlobalConfig()
    print(f"GlobalConfig: robot_width={gc.robot_width} rotation_diameter={gc.robot_rotation_diameter}")
    header = f"{'scene':<20} {'algo':<12} {'maxcost':>7} {'n>=100':>7} {'n50-99':>7} {'navmax':>7} {'path?':>6} {'through?':>9} {'clear_stop?':>11}"
    print(header)
    print("-" * len(header))

    for name, (obs_pts, bbox) in SCENES.items():
        pts = floor_points() if len(obs_pts) == 0 else np.vstack([floor_points(), obs_pts])
        cloud = cloud_from(pts)
        for algo in ("height_cost", "simple", "general"):
            try:
                cm = OCCUPANCY_ALGOS[algo](cloud)
                cells = bbox_cells(cm, bbox)
                maxc = int(cells.max()) if cells.size else -99
                lethal = int((cells >= 100).sum())
                mid = int(((cells >= 50) & (cells < 100)).sum())

                nav = make_navigation_map(cm, gc.robot_width * 1.1, "simple", "voronoi")
                ncells = bbox_cells(nav, bbox)
                navmax = int(ncells.max()) if ncells.size else -99

                path = min_cost_astar(nav, GOAL, START)
                if path is None or not path.poses:
                    pathres, through = "NONE", "-"
                else:
                    pathres = "yes"
                    through = "PLOWS" if path_hits_bbox(path, bbox) else "avoids"

                try:
                    pc = PathClearance(gc, straight_path())
                    pc.update_costmap(cm)
                    pc.update_pose_index(0)
                    stop = str(pc.is_obstacle_ahead())
                except ValueError:
                    # make_path_mask refuses paths >5% occupied -> in the real
                    # local planner this raises -> "error" -> replan (a stop).
                    stop = "STOP(mask)"
                print(f"{name:<20} {algo:<12} {maxc:>7} {lethal:>7} {mid:>7} {navmax:>7} {pathres:>6} {through:>9} {stop:>11}")
            except Exception as e:  # noqa: BLE001
                print(f"{name:<20} {algo:<12} ERROR: {type(e).__name__}: {e}")


def verify_patch_config():
    """Prove CostMapper.blueprint(algo="simple", config=...) validates and is
    actually used by _calculate_costmap — the exact patch we plan to apply."""
    from dimos.mapping.costmapper import CostMapper
    from dimos.mapping.pointclouds.occupancy import SimpleOccupancyConfig

    bp = CostMapper.blueprint(
        algo="simple",
        config=SimpleOccupancyConfig(min_height=0.15, max_height=2.0),
    )
    print(f"\npatch-config blueprint OK: {bp}")

    pts = np.vstack([floor_points(), box(1.0, 0.0, 0.5, 0.5, 1.2)])
    cm = OCCUPANCY_ALGOS["simple"](
        cloud_from(pts), min_height=0.15, max_height=2.0
    )
    cells = bbox_cells(cm, (0.75, 1.25, -0.25, 0.25))
    print(f"patched algo on tall box: maxcost={int(cells.max())} lethal={int((cells >= 100).sum())}")


if __name__ == "__main__":
    main()
    verify_patch_config()
