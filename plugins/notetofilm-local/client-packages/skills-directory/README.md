# 本地技能目录安装

这个包装把七个独立 Skill 放到用户明确指定的技能目录，并在明确指定的运行目录创建独立 Python 环境。它不修改其他插件，不自动猜测客户端路径，也不注册常驻服务。

```bash
python3 scripts/install.py --skills-dir /absolute/path/to/skills --runtime-dir /absolute/path/to/notetofilm-runtime
```

需要 Edge TTS 时增加 `--with-audio`。这个选项会在该独立环境安装音频依赖，需要能访问所配置的 Python 包源；普通安装本身不下载依赖。

客户端应支持发现目录中的 `SKILL.md`。安装后按照该客户端实际要求重新加载技能；安装器成功表示文件和本地命令已放好，不表示某个尚未使用的客户端已经完成发现。当前未声称验证所有客户端的原生插件商店或插件安装命令。

每个 Skill 的 `references/runtime.md` 会记录实际 `ntf` 路径和同套件各技能位置。项目参数必须使用明确路径，不依赖进程当前目录。

更新使用相同路径并增加 `--upgrade`。只接受安装器为同一插件建立的目录；已有同名但不属于本插件的技能会导致安装停止。安装器不会自动覆盖它们。
