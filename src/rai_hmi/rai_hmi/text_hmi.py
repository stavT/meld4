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
# See the License for the specific language goveself.rning permissions and
# limitations under the License.

import base64
import io
import logging
import sys
import time
from pathlib import Path
from pprint import pformat
from queue import Queue
from typing import Dict, List, Optional, Tuple, cast

import streamlit as st
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolCall,
    ToolMessage,
)
from PIL import Image
from pydantic import BaseModel
from rclpy.node import Node
from streamlit.delta_generator import DeltaGenerator

from rai.messages import HumanMultimodalMessage
from rai.node import RaiBaseNode
from rai.utils.artifacts import get_stored_artifacts
from rai_hmi.agent import initialize_agent
from rai_hmi.base import BaseHMINode
from rai_hmi.chat_msgs import EMOJIS, MissionMessage
from rai_hmi.ros import initialize_ros_nodes
from rai_hmi.text_hmi_utils import Memory

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger(__name__)

st.set_page_config(page_title="LangChain Chat App", page_icon="🦜", layout="wide")

MODEL = "gpt-4o"
MAX_DISPLAY = 5


# ---------- Cached Resources ----------
@st.cache_resource
def parse_args() -> Tuple[Optional[str], Optional[Path]]:
    robot_description_package = sys.argv[1] if len(sys.argv) > 1 else None
    allowlist = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    return robot_description_package, allowlist


@st.cache_resource
def initialize_memory() -> Memory:
    return Memory()


@st.cache_resource
def initialize_agent_streamlit(
    _hmi_node: BaseHMINode, _rai_node: RaiBaseNode, _memory: Memory
):
    return initialize_agent(_hmi_node, _rai_node, _memory)


@st.cache_resource
def initialize_mission_queue() -> Queue:
    return Queue()


@st.cache_resource
def initialize_ros_nodes_streamlit(
    _feedbacks_queue: Queue,
    robot_description_package: Optional[str],
    ros2_allowlist: Optional[Path],
):
    return initialize_ros_nodes(
        _feedbacks_queue, robot_description_package, ros2_allowlist
    )


def display_agent_message(
    message,  # TODO(boczekbartek): add typhint
    tool_chat_obj: Optional[DeltaGenerator] = None,
    no_expand: bool = False,
    tool_call: Optional[ToolCall] = None,
):
    """
    Display LLM message in streamlit UI.

    Args:
        message: The message to display
        tool_chat_obj: Pre-existing Streamlit object for the tool message.
        no_expand: Skip expanders due - Streamlit does not support nested expanders.
        tool_call: The tool call associated with the ToolMessage.

    """
    message.content = cast(str, message.content)  # type: ignore
    if not message.content:
        return  # Tool messages might not have any content, skip displaying them
    if isinstance(message, HumanMessage):
        if isinstance(
            message, HumanMultimodalMessage
        ):  # we do not handle user's images
            return
        st.chat_message("user", avatar=EMOJIS.human).markdown(message.content)
    elif isinstance(message, AIMessage):
        st.chat_message("bot", avatar=EMOJIS.bot).markdown(message.content)
    elif isinstance(message, ToolMessage):
        if tool_call is None:
            raise ValueError("`tool_call` argument is required for ToolMessage.")

        label = tool_call.name + " status: "
        status = EMOJIS.success if message.status == "success" else EMOJIS.failure
        if not tool_chat_obj:
            if not no_expand:
                tool_chat_obj = st.expander(label=label + status).chat_message(
                    "bot", avatar=EMOJIS.tool
                )
            else:
                tool_chat_obj = st.chat_message("bot", avatar=EMOJIS.tool)
        with tool_chat_obj:
            st.markdown(message.content)
            artifacts = get_stored_artifacts(message.tool_call_id)
            for artifact in artifacts:
                if "images" in artifact:
                    base_64_image = artifact["images"][0]
                    image = decode_base64_into_image(base_64_image)
                    st.image(image)
    elif isinstance(message, SystemMessage):
        return  # we do not handle system messages
    elif isinstance(message, MissionMessage):
        logger.info("Displaying mission message")
        with st.expander(label=message.STATUS):
            avatar, content = message.render_steamlit()
            st.chat_message("bot", avatar=avatar).markdown(content)
    else:
        raise ValueError("Unknown message type")


# ---------- Streamlit Application ----------
class SystemStatus(BaseModel):
    robot_database: bool
    system_prompt: bool


class Layout:
    """App has two columns: chat + robot state"""

    def __init__(self, robot_description_package) -> None:
        if robot_description_package:
            st.title(f"{robot_description_package.replace('_whoami', '')} chat app")
        else:
            st.title("ROS 2 Chat App")
            logging.warning(
                "No robot_description_package provided. Some functionalities may not work."
            )
        self.n_columns = 2
        self.tool_placeholders = dict()

    def draw(self, system_status: SystemStatus):
        self.draw_app_status_expander(system_status)
        self.draw_columns()

    def draw_app_status_expander(self, system_status: SystemStatus):
        with st.expander("System status", expanded=False):
            st.json(system_status.model_dump())

    def draw_columns(self):
        self.chat_column, self.mission_column = st.columns(self.n_columns)

    def create_tool_expanders(self, tool_calls: List[ToolCall]):
        with self.chat_column:
            for tool_call in tool_calls:
                tool_call = cast(ToolCall, tool_call)
                with st.expander(f"Tool call: {tool_call['name']}"):
                    st.markdown(f"Arguments: {tool_call['args']}")
                    self.tool_placeholders[tool_call["id"]] = st.empty()

    def write_tool_message(self, msg: ToolMessage, tool_call: ToolCall):
        with self.chat_column:
            display_agent_message(
                msg, self.tool_placeholders[msg.tool_call_id], tool_call=tool_call
            )

    def write_chat_msg(self, msg: BaseMessage):
        with self.chat_column:
            display_agent_message(msg)

    def write_mission_msg(self, msg: MissionMessage):
        with self.mission_column:
            logger.info(f'Mission said: "{msg}"')
            display_agent_message(msg)

    def show_chat(self, history, tool_calls: Dict[str, ToolCall]):
        with self.chat_column:
            self.__show_history(history, tool_calls)

    def show_mission(self, history, tool_calls: Dict[str, ToolCall]):
        with self.mission_column:
            self.__show_history(history, tool_calls)

    def __show_history(self, history, tool_calls):
        def display(message, no_expand=False):
            if isinstance(message, ToolMessage):
                display_agent_message(
                    message,
                    no_expand=no_expand,
                    tool_call=tool_calls[message.tool_call_id],
                )
            else:
                display_agent_message(message, no_expand=no_expand)

        for message in history:
            display(message)


class Chat:
    def __init__(self, memory: Memory, layout: Layout) -> None:
        self.memory = memory
        self.layout = layout

    def user(self, txt):
        logger.info(f'User said: "{txt}"')
        msg = HumanMessage(content=txt)
        self.memory.chat_memory.append(msg)
        self.layout.write_chat_msg(msg)

    def bot(self, msg):
        logger.info(f'Bot said: "{msg}"')
        self.memory.chat_memory.append(msg)
        self.layout.write_chat_msg(msg)

    def tool(self, msg):
        logger.info(f'Tool said: "{msg}"')
        self.memory.chat_memory.append(msg)
        tool_call = self.memory.tool_calls[msg.tool_call_id]
        self.layout.write_tool_message(msg, tool_call)

    def mission(self, msg: MissionMessage):
        logger.info(f'Mission said: "{msg}"')
        self.memory.add_mission(msg)
        self.layout.write_mission_msg(msg)


class Agent:
    def __init__(self, hmi_node: Node, rai_node: Node, memory) -> None:
        self.memory = memory
        self.agent = initialize_agent_streamlit(hmi_node, rai_node, self.memory)

    def stream(self):
        # Copy, because agent's memory != streamlit app memory. App memory is used to
        # recreate the page, agent might manipulate it's history to perform the task.
        # In simplest case it adds thinker message to the state.
        messages = self.memory.chat_memory.copy()
        logger.info(f"Sending messages:\n{pformat(messages)}")

        return self.agent.stream({"messages": messages})


class StreamlitApp:
    def __init__(self, robot_description_package, ros2_allowlist: Path) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)

        self.robot_description_package = robot_description_package

        self.layout = Layout(self.robot_description_package)
        self.memory = initialize_memory()
        self.chat = Chat(self.memory, self.layout)

        self.mission_queue = initialize_mission_queue()
        self.hmi_ros_node, self.rai_ros_node = self.initialize_node(
            self.mission_queue, robot_description_package, ros2_allowlist
        )
        self.agent = Agent(self.hmi_ros_node, self.rai_ros_node, self.memory)

    def update_mission(self):
        while True:
            if self.mission_queue.empty():
                time.sleep(0.5)
                continue
            logger.info("Got new mission update!")
            msg = self.mission_queue.get()
            self.chat.mission(msg)

    def run(self):
        system_status = self.get_system_status()
        self.layout.draw(system_status)

        self.recreate_from_memory()

        st.chat_input(
            "What is your question?", on_submit=self.prompt_callback, key="prompt"
        )

        self.update_mission()

    def get_system_status(self) -> SystemStatus:
        return SystemStatus(
            robot_database=self.hmi_ros_node.faiss_index is not None,
            system_prompt=self.hmi_ros_node.system_prompt != "",
        )

    def initialize_node(
        self, feedbacks_queue, robot_description_package, ros2_allowlist
    ):
        self.logger.info("Initializing ROS 2 node...")
        with st.spinner("Initializing ROS 2 nodes..."):
            hmi_node, rai_node = initialize_ros_nodes_streamlit(
                feedbacks_queue, robot_description_package, ros2_allowlist
            )
            self.logger.info("ROS 2 node initialized")
        return hmi_node, rai_node

    def recreate_from_memory(self):
        """
        Recreates the page from the memory. It is because Streamlit reloads entire page every widget action.
        See: https://docs.streamlit.io/get-started/fundamentals/main-concepts
        """
        self.layout.show_chat(self.memory.chat_memory, self.memory.tool_calls)
        self.layout.show_mission(self.memory.mission_memory, self.memory.tool_calls)

    def prompt_callback(self):
        prompt = st.session_state.prompt
        self.chat.user(prompt)

        message_placeholder = st.container()
        with self.layout.chat_column:
            with message_placeholder:
                with st.spinner("Thinking..."):
                    for state in self.agent.stream():
                        # logger.info(f"State:\n{pformat(state)}")
                        node_name = list(state.keys())[0]
                        if node_name == "thinker":
                            last_message = state[node_name]["messages"][-1]
                            self.chat.bot(last_message)
                            self.memory.register_tool_calls(last_message.tool_calls)
                            self.layout.create_tool_expanders(last_message.tool_calls)

                        elif node_name == "tools":
                            last_ai_msg_idx = 0
                            for message in state[node_name]["messages"]:
                                if isinstance(message, AIMessage):
                                    last_ai_msg_idx = state[node_name][
                                        "messages"
                                    ].index(message)

                            for message in state[node_name]["messages"][
                                last_ai_msg_idx + 1 :  # noqa: E203
                            ]:
                                if message.type == "tool":
                                    self.memory.tool_calls[message.tool_call_id] = (
                                        message
                                    )
                                    self.chat.tool(message)
                                else:
                                    break


def decode_base64_into_image(base64_image: str):
    image = Image.open(io.BytesIO(base64.b64decode(base64_image)))
    return image


if __name__ == "__main__":
    robot_description_package, ros2_allowlist = parse_args()
    app = StreamlitApp(robot_description_package, ros2_allowlist)
    app.run()
