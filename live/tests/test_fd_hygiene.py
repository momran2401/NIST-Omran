"""Child-process descriptor hygiene tests."""

import os
import tempfile

from core.shims import seal_open_fds_for_exec


def test_seal_open_fds_clears_inheritable_flag():
    with tempfile.TemporaryFile() as handle:
        fd = handle.fileno()
        os.set_inheritable(fd, True)
        assert os.get_inheritable(fd)

        seal_open_fds_for_exec()

        assert not os.get_inheritable(fd)
