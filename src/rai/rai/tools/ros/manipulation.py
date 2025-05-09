# Copyright (C) 2024 Robotec.AI
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from typing import Literal, Type

import numpy as np
import rclpy
import rclpy.callback_groups
import rclpy.executors
import rclpy.qos
import rclpy.subscription
import rclpy.task
from geometry_msgs.msg import Point, Pose, PoseStamped, Quaternion
from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field
from rai_open_set_vision.tools import GetGrabbingPointTool
from rclpy.client import Client
from rclpy.node import Node
from tf2_geometry_msgs import do_transform_pose

from rai.tools.utils import TF2TransformFetcher
from rai_interfaces.srv import ManipulatorMoveTo


class MoveToPointToolInput(BaseModel):
    x: float = Field(description="The x coordinate of the point to move to")
    y: float = Field(description="The y coordinate of the point to move to")
    z: float = Field(description="The z coordinate of the point to move to")
    task: Literal["grab", "drop"] = Field(
        description="Specify the intended action: use 'grab' to pick up an object, or 'drop' to release it. "
        "This determines the gripper's behavior during the movement."
    )


class MoveToPointTool(BaseTool):
    name: str = "move_to_point"
    description: str = (
        "Guide the robot's end effector to a specific point within the manipulator's operational space. "
        "This tool ensures precise movement to the desired location. "
        "While it confirms successful positioning, please note that it doesn't provide feedback on the "
        "success of grabbing or releasing objects. Use additional sensors or tools for that information."
    )

    node: Node
    client: Client

    manipulator_frame: str = Field(..., description="Manipulator frame")
    min_z: float = Field(default=0.135, description="Minimum z coordinate [m]")
    calibration_x: float = Field(default=0.0, description="Calibration x [m]")
    calibration_y: float = Field(default=0.0, description="Calibration y [m]")
    calibration_z: float = Field(default=0.0, description="Calibration z [m]")
    additional_height: float = Field(
        default=0.05, description="Additional height for the place task [m]"
    )

    # constant quaternion
    quaternion: Quaternion = Field(
        default=Quaternion(x=0.9238795325112867, y=-0.3826834323650898, z=0.0, w=0.0),
        description="Constant quaternion",
    )

    args_schema: Type[MoveToPointToolInput] = MoveToPointToolInput

    def __init__(self, node: Node, **kwargs):
        super().__init__(
            node=node,
            client=node.create_client(
                ManipulatorMoveTo,
                "/manipulator_move_to",
            ),
            **kwargs,
        )

    def _run(
        self,
        x: float,
        y: float,
        z: float,
        task: Literal["grab", "place"],
    ) -> str:
        pose_stamped = PoseStamped()
        pose_stamped.header.frame_id = self.manipulator_frame
        pose_stamped.pose = Pose(
            position=Point(x=x, y=y, z=z),
            orientation=self.quaternion,
        )

        if task == "place":
            pose_stamped.pose.position.z += self.additional_height

        pose_stamped.pose.position.x += self.calibration_x
        pose_stamped.pose.position.y += self.calibration_y
        pose_stamped.pose.position.z += self.calibration_z

        pose_stamped.pose.position.z = np.max(
            [pose_stamped.pose.position.z, self.min_z]
        )

        request = ManipulatorMoveTo.Request()
        request.target_pose = pose_stamped

        if task == "grab":
            request.initial_gripper_state = True  # open
            request.final_gripper_state = False  # closed
        else:
            request.initial_gripper_state = False  # closed
            request.final_gripper_state = True  # open

        future = self.client.call_async(request)
        self.node.get_logger().debug(
            f"Calling ManipulatorMoveTo service with request: x={request.target_pose.pose.position.x:.2f}, y={request.target_pose.pose.position.y:.2f}, z={request.target_pose.pose.position.z:.2f}"
        )

        rclpy.spin_until_future_complete(self.node, future, timeout_sec=5.0)

        if future.result() is not None:
            response = future.result()
            if response.success:
                return f"End effector successfully positioned at coordinates ({x:.2f}, {y:.2f}, {z:.2f}). Note: The status of object interaction (grab/drop) is not confirmed by this movement."
            else:
                return f"Failed to position end effector at coordinates ({x:.2f}, {y:.2f}, {z:.2f})."
        else:
            return f"Service call failed for point ({x:.2f}, {y:.2f}, {z:.2f})."


class GetObjectPositionsToolInput(BaseModel):
    object_name: str = Field(
        ..., description="The name of the object to get the positions of"
    )


class GetObjectPositionsTool(BaseTool):
    name: str = "get_object_positions"
    description: str = (
        "Retrieve the positions of all objects of a specified type in the target frame. "
        "This tool provides accurate positional data but does not distinguish between different colors of the same object type. "
        "While position detection is reliable, please note that object classification may occasionally be inaccurate."
    )

    target_frame: str
    source_frame: str
    camera_topic: str  # rgb camera topic
    depth_topic: str
    camera_info_topic: str  # rgb camera info topic
    node: Node
    get_grabbing_point_tool: GetGrabbingPointTool

    def __init__(self, node: Node, **kwargs):
        super(GetObjectPositionsTool, self).__init__(
            node=node, get_grabbing_point_tool=GetGrabbingPointTool(node=node), **kwargs
        )

    args_schema: Type[GetObjectPositionsToolInput] = GetObjectPositionsToolInput

    @staticmethod
    def format_pose(pose: Pose):
        return f"Centroid(x={pose.position.x:.2f}, y={pose.position.y:2f}, z={pose.position.z:2f})"

    def _run(self, object_name: str):
        transform = TF2TransformFetcher(
            target_frame=self.target_frame, source_frame=self.source_frame
        ).get_data()

        results = self.get_grabbing_point_tool._run(
            camera_topic=self.camera_topic,
            depth_topic=self.depth_topic,
            camera_info_topic=self.camera_info_topic,
            object_name=object_name,
        )

        poses = []
        for result in results:
            cam_pose = result[0]
            poses.append(
                Pose(position=Point(x=cam_pose[0], y=cam_pose[1], z=cam_pose[2]))
            )

        mani_frame_poses = []
        for pose in poses:
            mani_frame_pose = do_transform_pose(pose, transform)
            mani_frame_poses.append(mani_frame_pose)

        if len(mani_frame_poses) == 0:
            return f"No {object_name}s detected."
        else:
            return f"Centroids of detected {object_name}s in manipulator frame: [{', '.join(map(self.format_pose, mani_frame_poses))}]. Sizes of the detected objects are unknown."
