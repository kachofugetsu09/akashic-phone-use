"""真实 Core + UDS 协议回归；节点是故障 fixture，不代表 Android 真机验收。"""
import asyncio
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

CORE = Path(os.environ['AKASHIC_CORE_ROOT'])
sys.path.insert(0, str(CORE))
os.environ.update(AKASHIC_PLUGIN_DISTRIBUTION='', AKASHIC_EXECUTION_MODE='local')

from agent.plugin_composition import ServiceKey
from agent.plugins.manager import PluginManager
from tests.fixtures.plugin_workspace import initialize_plugin_workspace
from plugins.ledger.contract import BINDINGS, MESSAGE_CATALOG, OWNER_STATE, Input, Output, ContentPart, json_value
from plugins.turn_projection.contract import TURN_PROJECTION
from core.common.unix_socket import socket_alias_path
from websockets.asyncio.client import unix_connect
from websockets.exceptions import ConnectionClosed

WRITER_SOURCE = '''from agent.plugin_composition import ServiceKey
from plugins.ledger.contract import MESSAGE_WRITERS, Input, Output
from plugins.content.contract import CONTENT
api_version = 3
name = "audit_writer"
version = "1.0.0"
inject = (MESSAGE_WRITERS, CONTENT)
async def apply(ctx):
    writer = ctx.require(MESSAGE_WRITERS).bind(ctx, author="phone-audit", source="phone-audit",
        body_types=(Input, Output), content={"text":ctx.require(CONTENT).check_text})
    await ctx.provide(ServiceKey("audit.writer.v1"), (writer, ctx))
'''

async def wait_until(check):
    """等待真实回执更新；关键并发顺序由 Event 和设备锁门槛确定。"""
    async with asyncio.timeout(8):
        while not check():
            await asyncio.sleep(.01)

class Peer:
    """通过实际监听器 ACK/回复；可暂挂结果或只确认而不执行。"""
    def __init__(self, path, device):
        self.path, self.device = path, device
        self.requests = []
        self.display = None
        self.hold_action = False
        self.hold_finish = False
        self.execute_finish = True
        self.action_received = asyncio.Event()
        self.action_release = asyncio.Event()
        self.finish_received = asyncio.Event()
    async def start(self):
        """建立独立真实 WebSocket，并校验服务器握手。"""
        self.ws = await unix_connect(self.path, uri='ws://localhost/api/phone/ws')
        await self.ws.send(json.dumps({'type':'hello', 'device_id':self.device,
            'supports_ack':True, 'supports_snapshot_ref':True, 'shell_version':'0.5.0', 'display_mode':'virtual_display'}))
        welcome = json.loads(await self.ws.recv())
        assert welcome['type'] == 'welcome', welcome
        self.task = asyncio.create_task(self.run())
        return self
    async def reply(self, req, result, ok=True):
        await self.ws.send(json.dumps({'type':'response', 'request_id':req['request_id'],
            'ok':ok, 'result':result, 'state':{'virtual_display_id': 2 if self.display else None}}))
    async def run(self):
        """记录线上请求，以 fixture 状态显式表达动作和收尾效果。"""
        try:
            async for raw in self.ws:
                req=json.loads(raw)
                if req['type'] != 'request': continue
                self.requests.append(req)
                await self.ws.send(json.dumps({'type':'ack', 'request_id':req['request_id']}))
                if req['op']=='finish':
                    self.finish_received.set()
                    if self.execute_finish: self.display=None
                    if not self.hold_finish: await self.reply(req, {'kept':[], 'closed':[]})
                else:
                    self.display=req['payload'].get('text','active')
                    if self.hold_action:
                        self.action_received.set()
                        await self.action_release.wait()
                    await self.reply(req, {'message':'fixture action received', 'screen':{
                        'snapshot_ref':req['request_id'], 'display_id':2, 'nodes':[]}})
        except ConnectionClosed:
            pass
    async def close(self):
        self.action_release.set()
        await self.ws.close()
        await self.task

async def main():
    """在新临时 workspace 中加载实 Core，并验证生命周期与故障恢复。"""
    base=Path(tempfile.mkdtemp(prefix='phone-turn-'))
    workspace=base/'workspace'; sources=base/'sources'
    initialize_plugin_workspace(workspace)
    for name in ('ledger','content','tools','turn_projection','assets'):
        shutil.copytree(CORE/'plugins'/name, sources/name, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(Path(__file__).parent, sources/'phone_use',
        ignore=shutil.ignore_patterns('.git', '__pycache__', '.pytest_cache', 'tests'))
    (sources/'audit_writer').mkdir(); (sources/'audit_writer'/'plugin.py').write_text(WRITER_SOURCE)
    host=PluginManager([sources],workspace=workspace,installed_cache_root=base/'cache')
    peers=[]
    try:
        await host.load_all()
        root=host.live_root
        assert root
        from plugins.phone_use.contract import PHONE_BRIDGE
        bridge=root.context.require(PHONE_BRIDGE)
        control=root.context.require(ServiceKey('phone.control.v1'))
        writer, writer_ctx=root.context.require(ServiceKey('audit.writer.v1'))
        catalog=root.context.require(MESSAGE_CATALOG)
        projection=root.context.require(TURN_PROJECTION)
        ctx=control.ctx
        async with ctx.runtime_scope():
            binding=ctx.require(BINDINGS).bind(ServiceKey('phone.control.v1'), {'version':1}, contributors=(ctx,))
            state=ctx.require(OWNER_STATE).open(ctx)
        path=str(socket_alias_path(workspace/'runtime','phone-use.sock'))
        def phases(): return {k:dict(r.value) for k,r in state.list()}
        def phase(key):
            rec=state.read('phone-use:'+key)
            return rec.value['phase'] if rec else None
        async def begin(session,key,device,text):
            async with writer_ctx.runtime_scope():
                await writer(session).append_async(key+'-input',Input((ContentPart('text','audit fixture'),)))
            async with ctx.runtime_scope():
                return await control.action(key,binding,{'device_id':device,'action':'type_text','text':text,
                    '_phone':{'session_id':session,'source':'phone-audit','turn_input_id':key+'-input'}})
        async def close_turn(session,key):
            async with writer_ctx.runtime_scope():
                await writer(session).append_async(key+'-output',Output((ContentPart('text','done'),),'complete'))
        peer=await Peer(path,'shared-phone').start(); peers.append(peer)
        await begin('audit:A','A','shared-phone','A')
        peer.hold_action=True
        b=asyncio.create_task(begin('audit:B','B','shared-phone','B'))
        await peer.action_received.wait()
        await close_turn('audit:A','A')
        await wait_until(lambda: bool(bridge._locks['shared-phone']._waiters))
        peer.action_release.set(); await b
        await wait_until(lambda:phase('A')=='ended')
        bturn=projection.project(catalog.reader('audit:B').snapshot(),'phone-audit')[-1]
        result={'old_turn_closes_new_display':{'B_turn_status':bturn.status,'B_phase':phase('B'),
            'A_phase':phase('A'),'fixture_display_after':peer.display,
            'wire_requests':peer.requests}}
        assert bturn.status=='open' and phase('B')=='started' and peer.display == 'B'
        assert not any(req['op']=='finish' for req in peer.requests)
        assert state.read('phone-use:A').value['cleanup_outcome']=='not_sent'
        print('SHARED_DEVICE_PROTECTED',json.dumps(result,ensure_ascii=False),flush=True)
        # Old owner selectors are persisted fixture data, not an implemented cleanup capability.
        legacy=await Peer(path,'legacy-phone').start(); peers.append(legacy)
        await begin('audit:L','L','legacy-phone','L')
        original=state.read('phone-use:L')
        assert 'keep_packages' not in original.value and 'close_packages' not in original.value
        selectors={'keep_packages':['dev.fixture.keep'], 'close_packages':['dev.fixture.close']}
        async with ctx.runtime_scope():
            await state.transact_async(lambda tx: tx.save('phone-use:L', {**original.value, **selectors},
                expected_version=original.version))
        await close_turn('audit:L','L')
        await wait_until(lambda:phase('L')=='ended')
        finished=state.read('phone-use:L').value
        assert all(json_value(finished[k])==v for k,v in selectors.items())
        requests=[req for req in legacy.requests if req['op']=='finish']
        assert len(requests)==1 and requests[0]['payload']=={'reason':'turn_ended'} and legacy.display is None
        assert finished['cleanup_outcome']=='confirmed'
        result['legacy_selectors_ignored_without_rewriting']={'wire_finish':requests[0],
            'legacy_lists_preserved':True,'outcome':finished['cleanup_outcome']}
        print('LEGACY_SELECTORS_RELEASED',json.dumps(result['legacy_selectors_ignored_without_rewriting']),flush=True)
        # Reconnect alone must recover a known-unsent finish, with no new Message.
        off=await Peer(path,'offline-phone').start(); peers.append(off)
        await begin('audit:C','C','offline-phone','C')
        await off.close(); peers.remove(off)
        await wait_until(lambda:'offline-phone' not in bridge._sessions)
        before=len(off.requests)
        await close_turn('audit:C','C')
        await wait_until(lambda: state.read('phone-use:C').value.get('cleanup_outcome')=='not_sent')
        re=await Peer(path,'offline-phone').start(); peers.append(re); re.display='C retained fixture display'
        await wait_until(lambda:phase('C')=='ended')
        result['known_unsent_reconnect_recovered']={'phase':phase('C'),'pre_disconnect_wire':off.requests,
            'finish_sent_before_disconnect':any(x['op']=='finish' for x in off.requests),
            'wire_after_reconnect':re.requests,'fixture_display_after':re.display}
        assert before==1 and len(re.requests)==1 and re.requests[0]['op']=='finish' and re.display is None
        assert state.read('phone-use:C').value['cleanup_outcome']=='confirmed'
        assert 'error' not in state.read('phone-use:C').value
        print('KNOWN_UNSENT_RECOVERED',json.dumps(result['known_unsent_reconnect_recovered'],ensure_ascii=False),flush=True)
        parked=await Peer(path,'parked-phone').start(); peers.append(parked)
        await begin('audit:P','P','parked-phone','P')
        await parked.close(); peers.remove(parked)
        await wait_until(lambda:'parked-phone' not in bridge._sessions)
        await close_turn('audit:P','P')
        await wait_until(lambda:state.read('phone-use:P').value.get('cleanup_outcome')=='not_sent')
        # Real 15-second result timeout, no monkeypatched timeout; ACK delivered, no response.
        unknown=await Peer(path,'unknown-phone').start(); peers.append(unknown)
        await begin('audit:U','U','unknown-phone','U')
        unknown.hold_finish=True; unknown.execute_finish=False
        await close_turn('audit:U','U')
        await asyncio.wait_for(unknown.finish_received.wait(),8)
        # The pending U finish holds the follower, not the other device's real RPC path.
        parked_new=await Peer(path,'parked-phone').start(); peers.append(parked_new)
        await begin('audit:P2','P2','parked-phone','P2')
        async with asyncio.timeout(20):
            while phase('U')!='cleanup_unknown': await asyncio.sleep(.02)
        assert state.read('phone-use:U').value['cleanup_outcome']=='unknown'
        assert '结果不确定' in state.read('phone-use:U').value['error']
        await wait_until(lambda:phase('P')=='ended')
        assert parked_new.display=='P2' and len(parked_new.requests)==1
        assert state.read('phone-use:P').value['cleanup_outcome']=='not_sent'
        result['reconnect_new_owner_protected']={'old_phase':phase('P'),'new_phase':phase('P2'),
            'wire_requests':parked_new.requests,'fixture_display':parked_new.display}
        await unknown.close(); peers.remove(unknown)
        unknown_new=await Peer(path,'unknown-phone').start(); peers.append(unknown_new)
        sentinel=await Peer(path,'sentinel').start(); peers.append(sentinel)
        await begin('audit:S','S','sentinel','S'); await close_turn('audit:S','S')
        await wait_until(lambda:phase('S')=='ended')
        assert not unknown_new.requests
        result['unknown_not_replayed']={'phase':phase('U'),'wire_requests':unknown.requests,
            'fixture_display_after':unknown.display,'note':'ACK delivered; fixture intentionally did not execute or respond; Core cannot know effect.'}
        print('UNKNOWN_NOT_REPLAYED',json.dumps(result['unknown_not_replayed'],ensure_ascii=False),flush=True)
        # Cancellation after the real finish frame must remain unknown across actual generation reload.
        cancel=await Peer(path,'cancel-phone').start(); peers.append(cancel)
        await begin('audit:X','X','cancel-phone','X')
        cancel.hold_finish=True
        await close_turn('audit:X','X')
        await asyncio.wait_for(cancel.finish_received.wait(),8)
        assert state.read('phone-device:cancel-phone').value['cleanup_outcome']=='unknown'
        old_bridge=bridge
        source=sources/'phone_use'/'plugin.py'
        source.write_text(source.read_text()+'\n# Isolated generation reload probe.\n')
        changes=await host.reconcile_changed(plugin_ids=frozenset({'phone-use'}))
        bridge=root.context.require(PHONE_BRIDGE)
        control=root.context.require(ServiceKey('phone.control.v1')); ctx=control.ctx
        node_error_type=type(bridge).rpc.__globals__['PhoneNodeError']
        async with ctx.runtime_scope(): state=ctx.require(OWNER_STATE).open(ctx)
        assert bridge is not old_bridge and not old_bridge._sessions
        assert not old_bridge._closing_transports
        async with ctx.runtime_scope():
            async with ctx.require(BINDINGS).open(binding,ServiceKey('phone.control.v1')) as (bound,_):
                assert bound is control and bound.bridge is bridge
        after_cancel=await Peer(path,'cancel-phone').start(); peers.append(after_cancel)
        await wait_until(lambda:phase('X')=='cleanup_unknown')
        assert not after_cancel.requests
        result['reload_cancellation_not_replayed']={'phase':phase('X'),'wire_after_reload':after_cancel.requests,
            'old_bridge_closed':not old_bridge._sessions,'changes':changes}
        print('RELOAD_CANCELLATION_PROTECTED',json.dumps(result['reload_cancellation_not_replayed'],ensure_ascii=False),flush=True)
        # A real SQLite writer lock holds the owner transaction beyond the RPC deadline.
        budget=await Peer(path,'budget-phone').start(); peers.append(budget)
        async with writer_ctx.runtime_scope():
            await writer('audit:D').append_async('D-input',Input((ContentPart('text','deadline fixture'),)))
        locked=sqlite3.connect(workspace/'sessions.db')
        locked.execute('BEGIN IMMEDIATE')
        loop=asyncio.get_running_loop()
        release=loop.call_later(.25,locked.rollback)
        started=loop.time()
        try:
            async with ctx.runtime_scope():
                try:
                    await control.action('D',binding,{'device_id':'budget-phone','action':'type_text','text':'D',
                        'timeout_ms':100,'_phone':{'session_id':'audit:D','source':'phone-audit','turn_input_id':'D-input'}})
                except node_error_type as error:
                    assert error.outcome=='not_sent',repr(error)
                else:
                    raise AssertionError('expired owner IO sent an action')
            assert loop.time()-started>=.1 and not budget.requests
        finally:
            release.cancel(); locked.rollback(); locked.close()
        result['owner_io_deadline']={'wire_requests':budget.requests,'outcome':'not_sent',
            'elapsed_ms':round((loop.time()-started)*1000)}
        print('OWNER_IO_DEADLINE',json.dumps(result['owner_io_deadline']),flush=True)
        result['workspace']=str(workspace); result['owner_records']=phases()
        (base/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
    finally:
        for peer in peers: await peer.close()
        await host.terminate_all()
    print('AUDIT_COMPLETE',base,flush=True)

asyncio.run(main())
