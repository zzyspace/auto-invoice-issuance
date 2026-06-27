from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from app.models import AppConfig, StoreConfig
from app.utils import backup_existing_file, ensure_parent_dir


class PortalWorkbookSyncError(RuntimeError):
    pass


class PortalWorkbookSyncer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    def sync_store_workbook(self, store: StoreConfig) -> None:
        if not self.config.portal_sync_from_server:
            return
        remote_host = (self.config.portal_sync_remote_host or "").strip()
        remote_output_dir = (self.config.portal_sync_remote_output_dir or "").strip()
        if not remote_host or not remote_output_dir:
            raise PortalWorkbookSyncError(
                "TAX_PORTAL_SYNC_FROM_SERVER=true requires TAX_PORTAL_REMOTE_HOST and TAX_PORTAL_REMOTE_OUTPUT_DIR."
            )
        scp_bin = shutil.which("scp")
        if not scp_bin:
            raise PortalWorkbookSyncError("`scp` is not available on this machine.")
        remote_path = f"{remote_host}:{remote_output_dir.rstrip('/')}/{store.output_xlsx_path.name}"
        local_path = store.output_xlsx_path
        ensure_parent_dir(local_path)
        temp_path = local_path.with_name(local_path.name + ".downloading")
        command = self._build_command(scp_bin, remote_path, temp_path)
        try:
            subprocess.run(command, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            raise PortalWorkbookSyncError(
                f"Failed to sync workbook for {store.store_key} from server: {stderr or exc}"
            ) from exc
        backup_existing_file(local_path, self.config.backups_root / "sync" / store.store_key)
        temp_path.replace(local_path)

    def _build_command(self, scp_bin: str, remote_path: str, local_path: Path) -> list[str]:
        command = [scp_bin]
        if self.config.portal_sync_ssh_port and self.config.portal_sync_ssh_port != 22:
            command.extend(["-P", str(self.config.portal_sync_ssh_port)])
        if self.config.portal_sync_ssh_key_path:
            command.extend(["-i", str(self.config.portal_sync_ssh_key_path)])
        command.extend(["-o", f"BatchMode={'yes' if self.config.portal_sync_batch_mode else 'no'}"])
        command.extend(
            [
                "-o",
                f"StrictHostKeyChecking={'yes' if self.config.portal_sync_strict_host_key_checking else 'no'}",
            ]
        )
        command.extend(
            [
                "-o",
                f"ConnectTimeout={self.config.portal_sync_connect_timeout_seconds}",
            ]
        )
        command.extend([remote_path, str(local_path)])
        return command
