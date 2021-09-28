'''
Copyright (C) 2017-2021  Bryant Moscon - bmoscon@gmail.com

Please see the LICENSE file for the terms and conditions
associated with this software.
'''
import asyncio
import atexit
from collections import defaultdict
import functools
import ast

from aiofile import AIOFile
from yapic import json

from cryptofeed.defines import HUOBI, UPBIT, OKEX, OKCOIN
from cryptofeed.exchanges import EXCHANGE_MAP
import gzip
import shutil
import datetime
import pandas as pd
import os
import sys

def bytes_string_to_bytes(string):
    tree = ast.parse(string)
    return tree.body[0].value.s


def playback(feed: str, filenames: list):
    return asyncio.run(_playback(feed, filenames))


async def _playback(feed: str, filenames: list):
    callbacks = defaultdict(int)

    class FakeWS:
        def __init__(self, filenames):
            self.conn_type = 'wss'
            self.uuid = "1"
            self.cache = defaultdict(list)

            for filename in filenames:
                if 'http' in filename:
                    with open(filename, 'r') as fp:
                        for line in fp.readlines():
                            if line.startswith('http'):
                                file_url, data = line.split(' -> ')
                                _, msg = data.split(": ", 1)
                                self.cache[file_url].append(msg)

        async def write(self, *args, **kwargs):
            pass

        async def read(self, url):
            return self.cache[url].pop(0)

    ws = FakeWS(filenames)
    symbol_data = []
    sub = None
    for f in filenames:
        if 'ws' not in f and 'http' not in f:
            exchange = f.rsplit("/", 1)[1]
            exchange = exchange.split(".", 1)[0]
            with open(f, 'r') as fp:
                for line in fp.readlines():
                    if 'configuration' in line:
                        sub = json.loads(line.split(": ", 1)[1])
                    if line == "\n":
                        continue
                    line = line.split(": ", 1)[1]
                    symbol_data.append(json.loads(line.strip()))

    def symbol_helper(*args, **kwargs):
        ret = symbol_data.pop(0)
        return ret

    from cryptofeed.connection import HTTPAsyncConn, HTTPSync
    http_async_conn_read = HTTPAsyncConn.read
    http_sync_read = HTTPSync.read
    HTTPAsyncConn.read = ws.read
    HTTPSync.read = symbol_helper

    async def internal_cb(*args, **kwargs):
        callbacks[kwargs['cb_type']] += 1

    feed = EXCHANGE_MAP[feed](subscription=sub)
    for cb_type, handler in feed.callbacks.items():
        f = functools.partial(internal_cb, cb_type=cb_type)
        handler.append(f)

    for _, sub, handler in feed.connect():
        await sub(ws)

    counter = 0
    filenames = [filename for filename in filenames if '.ws.' in filename]
    for filename in filenames:
        with open(filename, 'r') as fp:
            for line in fp:
                if line == "\n":
                    continue
                start = line[:3]
                if start == 'wss':
                    continue
                if start == 'htt':
                    counter += 1
                    continue

                try:
                    timestamp, message = line.split(": ", 1)
                    counter += 1

                    if OKCOIN in filename or OKEX in filename:
                        if message.startswith('b\'') or message.startswith('b"'):
                            message = bytes_string_to_bytes(message)
                    elif HUOBI in filename:
                        message = bytes_string_to_bytes(message)
                    elif UPBIT in filename:
                        if message.startswith('b\'') or message.startswith('b"'):
                            message = message.strip()[2:-1]

                    await handler(message, ws, timestamp)
                except Exception:
                    print("Playback failed on message:", message)
                    raise
    feed.stop()
    await feed.shutdown()

    HTTPAsyncConn.read = http_async_conn_read
    HTTPSync.read = http_sync_read
    return {'messages_processed': counter, 'callbacks': dict(callbacks)}


class AsyncFileCallback:
    def __init__(self, path, length=10000, rotate=1024 * 1024 * 100, time_based_flushing=False):
        self.path = path
        self.length = length
        self.data = defaultdict(list)
        self.rotate = rotate
        self.count = defaultdict(int)
        self.pointer = defaultdict(int)
        atexit.register(self.__del__)
        self.time_based_flushing = time_based_flushing
        self.time_range = pd.date_range(start='00:00:00', end='23:59:59', freq='T').time

    def __del__(self):
        self.stop()

    def stop(self):
        for uuid in list(self.data.keys()):
            with open(f"{self.path}/{uuid}.{self.count[uuid]}", 'a') as fp:
                fp.write("\n".join(self.data[uuid]) + "\n")
                self.data[uuid] = []
                fp.flush()

    def write_header(self, uuid, data):
        with open(f"{self.path}/{uuid}.{0}", 'a') as fp:
            fp.write(f"configuration: {data}\n")
            fp.flush()

    async def write(self, uuid):
        p = f"{self.path}/{uuid}.{self.count[uuid]}"
        async with AIOFile(p, mode='a') as fp:
            r = await fp.write("\n".join(self.data[uuid]) + "\n", offset=self.pointer[uuid])
            self.pointer[uuid] += r
            self.data[uuid] = []
            await fp.fsync()

        current_time = datetime.datetime.now()
        time_range_str = [x.isoformat(timespec='seconds') for x in self.time_range]
        if current_time.hour == 0 and current_time.minute == 0 and current_time.second == 0:
            with open(p, 'rb') as f_in:
                with gzip.open(p + '.gz', 'wb') as f_out:
                    shutil.copyfileobj(f_in, f_out)
            os.execv(sys.executable, ['python'] + sys.argv)

        if self.time_based_flushing:
            if current_time.time().isoformat(timespec='seconds') in time_range_str:
                with open(p, 'rb') as f_in:
                    with gzip.open(p + '.gz', 'wb') as f_out:
                        shutil.copyfileobj(f_in, f_out)
                self.count[uuid] += 1
                self.pointer[uuid] = 0
        else:
            if self.pointer[uuid] >= self.rotate:
                self.count[uuid] += 1
                self.pointer[uuid] = 0

    async def __call__(self, data: str, timestamp: float, uuid: str, endpoint: str = None, send: str = None, connect: str = None):
        if endpoint:
            self.data[uuid].append(f"{endpoint} -> {timestamp}: {data}")
        elif send:
            self.data[uuid].append(f"{send} <- {timestamp}: {data}")
        elif connect:
            self.data[uuid].append(f"{connect} <-> {timestamp}")
        else:
            self.data[uuid].append(f"{timestamp}: {data}")

        if len(self.data[uuid]) >= self.length:
            await asyncio.create_task(self.write(uuid))

    def sync_callback(self, data: str, timestamp: float, uuid: str, endpoint: str = None, send: str = None, connect: str = None):
        if endpoint:
            w = f"{endpoint} -> {timestamp}: {data}"
        elif send:
            w = f"{send} <- {timestamp}: {data}"
        elif connect:
            w = f"{connect} <-> {timestamp}"
        else:
            w = f"{timestamp}: {data}"

        with open(f"{self.path}/{uuid}.{0}", 'a') as fp:
            fp.write(w + "\n")
            fp.flush()
