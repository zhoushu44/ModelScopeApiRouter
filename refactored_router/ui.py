import sys
import threading
import time
from pathlib import Path
from rich.console import Console
from rich.table import Table
from rich.live import Live
from rich import box

# --- 添加模块路径 ---
current_dir = Path(__file__).parent
if str(current_dir) not in sys.path:
    sys.path.insert(0, str(current_dir))
# ------------------

from settings import config
from stats import stats_service
from schema import CallRecord

class Dashboard:
    def __init__(self):
        self.console = Console()
        self.live = Live(self.console, auto_refresh=False, vertical_overflow="visible")
        self.running = False
        self._thread = None

    def start(self):
        self.running = True
        try:
            self.live.start()
            self.live.update(self._generate_table(), refresh=True)
            self._thread = threading.Thread(target=self._updater, daemon=True)
            self._thread.start()
        except Exception:
            pass

    def stop(self):
        self.running = False
        try:
            self.live.stop()
        except Exception:
            pass

    def _print(self, renderable):
        try:
            if self.running:
                self.live.console.print(renderable)
            else:
                self.console.print(renderable)
        except Exception:
            pass

    def log_request(self, model_id: str, is_stream: bool):
        self._print(f"\n📨 Request: {model_id} (Stream: {is_stream})")

    def log_attempt(self, model_name: str):
        self._print(f"👉 Trying: {model_name}...")

    def log_result(self, record: CallRecord):
        try:
            snapshot = stats_service.get_snapshot()
            limits = snapshot['limits']
            stats_data = snapshot['stats']
            
            limit = limits.get(record.model_name, 50)
            calls = stats_data.get(record.model_name, {}).get('calls', 0)

            if record.success:
                status = "SUCCESS"
                msg = f"Time: {record.response_time:.2f}s"
            else:
                status = "FAILED"
                msg = record.error_message

            self._print(
                f"  ↳ {status} {record.model_name} | "
                f"Use: {calls}/{limit} | {msg}"
            )
            self.refresh()
        except Exception:
            pass

    def log_error(self, msg: str):
        self._print(f"❌ {msg}")

    def refresh(self):
        try:
            if self.running:
                self.live.update(self._generate_table(), refresh=True)
        except Exception:
            pass

    def _updater(self):
        while self.running:
            self.refresh()
            time.sleep(0.5)

    def _generate_table(self) -> Table:
        try:
            table = Table(
                title="🤖 ModelScope Router",
                box=box.ROUNDED,
                caption=f"Port: {config.PORT} | Status: Running",
                expand=True,
                border_style="bright_black"
            )
            table.add_column("Model Name", style="cyan", no_wrap=True)
            table.add_column("Usage", justify="center")
            table.add_column("Success Rate", justify="center")
            table.add_column("Status", justify="center")

            snapshot = stats_service.get_snapshot()
            stats_data = snapshot['stats']
            limits = snapshot['limits']

            for model in config.MODELS:
                name = model['name']
                st = stats_data.get(name, {})
                calls = st.get('calls', 0)
                success = st.get('success_calls', 0)
                limit = limits.get(name, 50)
                is_limited = st.get('is_limited', False)
                
                rate = (success / calls * 100) if calls > 0 else 0
                
                if is_limited:
                    status = "🔴 LIMITED"
                else:
                    status = "🟢 Active"

                table.add_row(
                    name,
                    f"{calls}/{limit}",
                    f"{rate:.1f}%",
                    status
                )
            return table
        except Exception:
            return Table(title="🤖 ModelScope Router")

# 全局 UI 实例
dashboard = Dashboard()
