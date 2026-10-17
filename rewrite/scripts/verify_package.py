"""在隔离解释器内确认全部项目模块从 wheel 加载，不能由旧源码补齐。"""
import importlib
import pkgutil
import sys
from pathlib import Path


def main():
    if not sys.flags.isolated:
        raise RuntimeError('请使用 python -I 执行')
    wheel = Path(sys.argv[1]).resolve(strict=True)
    sys.path.insert(0, str(wheel))
    import helpdesk

    names = [module.name for module in pkgutil.walk_packages(helpdesk.__path__, helpdesk.__name__ + '.')]
    for name in names:
        module = importlib.import_module(name)
        if str(wheel) not in str(module.__file__):
            raise RuntimeError('模块来自 wheel 之外：' + name)
    from helpdesk.graph.baseline import PROMPTS

    assert len(PROMPTS) == 6
    print({'wheel_modules': len(names), 'frozen_prompts': len(PROMPTS), 'old_runtime_imported': False})


if __name__ == '__main__':
    main()
