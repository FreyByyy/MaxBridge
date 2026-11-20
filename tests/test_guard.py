import pytest
from pymax.connection.connection import ConnectionManager
from pymax.protocol import Command, Opcode, OutboundFrame

from bot import readonly_guard


def frame(op: Opcode, cmd: int = Command.REQUEST) -> OutboundFrame:
    return OutboundFrame(ver=10, opcode=int(op), cmd=cmd, seq=1, payload={})


@pytest.mark.asyncio
@pytest.mark.parametrize("op", [Opcode.MSG_SEND, Opcode.MSG_EDIT, Opcode.MSG_DELETE, Opcode.CHAT_MARK, Opcode.MSG_REACTION])
async def test_write_requests_blocked(op):
    readonly_guard.install()
    with pytest.raises(readonly_guard.ReadOnlyViolation):
        await ConnectionManager.request(None, frame(op))  # type: ignore[arg-type]
    with pytest.raises(readonly_guard.ReadOnlyViolation):
        await ConnectionManager.send(None, frame(op))  # type: ignore[arg-type]


@pytest.mark.parametrize("op", [Opcode.PING, Opcode.LOGIN, Opcode.MSG_GET, Opcode.CHAT_MEMBERS, Opcode.FILE_DOWNLOAD])
def test_read_requests_allowed(op):
    assert not readonly_guard.is_blocked(int(op))


def test_client_send_methods_hit_guard():
    # Если PyMax когда-нибудь поменяет номера, тест напомнит перепроверить список.
    for name in ("MSG_SEND", "MSG_EDIT", "MSG_DELETE", "CHAT_MARK"):
        assert readonly_guard.is_blocked(int(Opcode[name]))
