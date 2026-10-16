"""从旧环境与模型 TOML 一次性导入；不启动任务，也不修改旧文件。"""
import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import Config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='config/config.toml', help='新完整配置的路径，文件必须尚不存在')
    parser.add_argument('--data-dir', help='已有 data 目录；省略时使用旧 VIDEO_DATA_DIR 或默认目录')
    args = parser.parse_args()
    path = Path(args.output).expanduser().resolve()
    if path.exists():
        parser.error('目标已存在；请保留原文件并选择新的输出路径')
    env = {**os.environ, 'NOTETOFILM_CONFIG_FILE': str(path)}
    if args.data_dir:
        env['VIDEO_DATA_DIR'] = str(Path(args.data_dir).expanduser().resolve())
    Config.load(env)
    print('完整配置已保存。停止旧服务后，使用新配置文件启动；旧配置及任务数据已保留。')


if __name__ == '__main__':
    main()
