import os
from pathlib import Path

from app.app import create_app
from app.config import Config
from app.providers import Providers
from app.render import run_process
from app.tasks import Tasks


class OfflineProviders(Providers):
    def plan(self, script, brief=None):
        return [script]

    def image(self, narration, output):
        run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                     'color=c=blue:s=320x180', '-frames:v', '1', str(output)], 30)

    def audio(self, narration, output):
        run_process(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                     'sine=frequency=440:duration=1.2', '-c:a', 'libmp3lame', str(output)], 30)


settings = Config(data=Path(os.environ['VIDEO_DATA_DIR']), token='offline-api-admin',
                  base='https://unused.invalid/v1', key='offline-api-model')
app = create_app(settings, Tasks(settings, providers=OfflineProviders(settings)))
