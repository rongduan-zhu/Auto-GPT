import asyncio
import json
import re
from contextlib import contextmanager
from multiprocessing import Process, Queue
from time import sleep
from typing import Optional

from websockets.server import serve

from autogpt.logs import logger


def _remove_color_codes(s):
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    return ansi_escape.sub("", s)


class Channel:
    def __init__(self, client_queue: Queue, server_queue: Queue):
        self.client_queue = client_queue
        self.server_queue = server_queue


class SerializableMessage:
    def to_json(self):
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, json_string):
        return cls(**json.loads(json_string))


class AutoGptResponse(SerializableMessage):
    WAIT_FOR_INPUT_TYPE = "wait_for_input"
    RESPONSE_TYPE = "response"

    def __init__(self, text: Optional[str] = None, type: str = RESPONSE_TYPE):
        self.text = text
        self.type = type


class UserInput(SerializableMessage):
    RESPONSE_TYPE = "user_input"
    EXIT_TYPE = "exit"

    def __init__(self, type: str, prompt: str):
        self.prompt = prompt
        self.type = type


async def receive_messages(websocket, queue: Queue):
    async for data in websocket:
        logger.debug(f"Received message from client: {data}")
        user_input = UserInput.from_json(data)  # We expect JSON-formatted data
        queue.put(user_input)


async def send_messages(websocket, queue: Queue):
    while True:
        loop = asyncio.get_event_loop()
        message: AutoGptResponse = await loop.run_in_executor(None, queue.get)
        if message.text:
            message.text = _remove_color_codes(message.text)
        await websocket.send(message.to_json())


# Define the server function
async def _server(client_queue, server_queue, websocket):
    logger.debug("Websocket server started")
    t1 = asyncio.create_task(receive_messages(websocket, client_queue))
    t2 = asyncio.create_task(send_messages(websocket, server_queue))
    logger.debug("Waiting for threads to finish")
    await t1
    await t2


def _start_server(client_queue, server_queue, host: str, port: int):
    async def start_websocket_server():
        async with serve(server_with_queue, host, port):
            logger.debug("Websocket server started")
            await asyncio.Future()  # run forever

    async def server_with_queue(websocket):
        logger.debug("Starting server with queue")
        try:
            await _server(client_queue, server_queue, websocket)
        except Exception as e:
            logger.error(message=f"Error in server: {e}")

    logger.debug("Starting server")
    try:
        asyncio.get_event_loop().run_until_complete(start_websocket_server())
        asyncio.get_event_loop().run_forever()
        logger.debug("Server ended")
    except Exception as e:
        logger.error(message=f"Error when starting asyncio: {e}")


def send_to_server(channel: Optional[Channel]):
    def _send_to_server(message, type=AutoGptResponse.RESPONSE_TYPE):
        if channel:
            logger.debug(f"sending {message} to server")
            channel.server_queue.put(AutoGptResponse(message, type))

    return _send_to_server


@contextmanager
def start_server_in_background(
    host: Optional[str], port: Optional[int]
) -> Optional[Channel]:
    client_queue = Queue()
    server_queue = Queue()
    p = None
    channel = None
    if host and port:
        logger.info(f"Starting server on {host}:{port}")
        channel = Channel(client_queue, server_queue)
        p = Process(
            target=_start_server,
            args=(
                client_queue,
                server_queue,
                host,
                port,
            ),
        )
        p.start()
        logger.debug(f"Started server process {p.exitcode}")
    else:
        logger.info(f"No server and port specified, not starting server")
    try:
        yield channel
    finally:
        if p:
            p.terminate()
            p.join()
