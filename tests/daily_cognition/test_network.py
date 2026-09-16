"""Actual socket lifetime, commit confirmation and shared quiet-time admission."""
import asyncio
import socket
import threading
import unittest
from companion_memory.provider.daily_network import DailyNetwork,NetworkPermit
from companion_memory.persistence.owned_statements import OwnerFailure

class DailyNetworkTests(unittest.IsolatedAsyncioTestCase):
    async def test_consumer_cleanup_blocks_other_account_and_starts_shared_gap(self):
        clock=[100.0];network=DailyNetwork(lambda key:True,('metered','allocated'),monotonic=lambda:clock[0])
        try:
            network.resume();permit=network.reserve('request','key','metered',300.0,consumer_required=True)
            self.assertEqual(await network.start(permit,lambda:7),7);await network.wait_io(permit)
            clock[0]=110.0;network.confirm_terminal(permit)
            self.assertTrue(network.observation().consumer_cleanup_pending)
            self.assertFalse(network.observation().io_pending);self.assertFalse(network.observation().terminal_confirmation_pending)
            clock[0]=200.0
            with self.assertRaises(OwnerFailure):network.reserve('next','next','allocated',300.0)
            network.confirm_consumed(permit)
            self.assertEqual(network.observation().quiet_until,230.0)
            self.assertFalse(network.observation().occupied)
            clock[0]=229.999
            with self.assertRaises(OwnerFailure):network.reserve('next','next','allocated',300.0)
            clock[0]=230.0;next_permit=network.reserve('next','next','allocated',300.0)
            network.cancel_unregistered(next_permit)
        finally:network.close()

    async def test_real_socket_tail_cannot_release_on_timeout_or_close(self):
        clock=[100.0];network=DailyNetwork(lambda key:True,('metered','allocated'),monotonic=lambda:clock[0])
        started=threading.Event();release=threading.Event();listener=socket.socket()
        listener.bind(('127.0.0.1',0));listener.listen(1);listener.settimeout(5)
        errors=[]
        def server():
            try:
                connection,_=listener.accept()
                with connection:
                    connection.settimeout(5)
                    self.assertEqual(connection.recv(4),b'ping')
                    started.set()
                    if not release.wait(5):raise TimeoutError()
                    connection.sendall(b'pong')
            except BaseException as error:errors.append(error)
        thread=threading.Thread(target=server);thread.start()
        def exchange():
            with socket.create_connection(listener.getsockname(),timeout=5) as connection:
                connection.sendall(b'ping')
                return connection.recv(4)
        try:
            with self.assertRaises(TypeError):NetworkPermit()
            with self.assertRaises(OwnerFailure):network.reserve('request','key','metered',200.0)
            network.resume();permit=network.reserve('request','key','metered',200.0)
            future=network.start(permit,exchange)
            for _ in range(200):
                if started.is_set():break
                await asyncio.sleep(.005)
            self.assertTrue(started.is_set())
            with self.assertRaises(asyncio.TimeoutError):await asyncio.wait_for(future,.01)
            observation=network.observation()
            self.assertTrue(observation.io_pending);self.assertTrue(observation.terminal_confirmation_pending)
            clock[0]=101.0;network.confirm_terminal(permit)
            self.assertTrue(network.observation().io_pending)
            with self.assertRaises(OwnerFailure):network.reserve('other','other','allocated',200.0)
            self.assertTrue(network.close().io_pending)
            clock[0]=102.0;release.set();await network.wait_io(permit)
            observation=network.observation()
            self.assertFalse(observation.io_pending);self.assertFalse(observation.occupied)
            self.assertEqual(observation.quiet_until,132.0)
            with self.assertRaises(OwnerFailure):network.reserve('late','late','allocated',200.0)
        finally:
            release.set();network.close();thread.join(5);listener.close()
        self.assertFalse(thread.is_alive());self.assertEqual(errors,[])

    async def test_gap_starts_at_later_commit_and_account_fence_is_restored_paused(self):
        clock=[100.0];network=DailyNetwork(lambda key:True,('metered','allocated'),monotonic=lambda:clock[0])
        try:
            network.block_account('metered');network.resume()
            with self.assertRaises(OwnerFailure):network.reserve('unknown','unknown','metered',200.0)
            permit=network.reserve('request','key','allocated',200.0)
            self.assertEqual(await network.start(permit,lambda:7),7)
            await network.wait_io(permit)
            self.assertTrue(network.observation().occupied)
            clock[0]=110.0;network.confirm_terminal(permit)
            clock[0]=139.999
            with self.assertRaises(OwnerFailure):network.reserve('next','next','allocated',200.0)
            clock[0]=140.0;next_permit=network.reserve('next','next','allocated',200.0)
            network.pause();called=[]
            with self.assertRaises(OwnerFailure):network.start(next_permit,lambda:called.append('side effect'))
            network.confirm_terminal(next_permit)
            self.assertEqual(called,[]);self.assertFalse(network.observation().io_pending)
        finally:network.close()
