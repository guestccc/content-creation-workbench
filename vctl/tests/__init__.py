"""
vct 的自动化测试包。

只用标准库 unittest，不引入 pytest —— vct 本体坚持最小依赖，
测试也不该是第一处例外。

运行全部测试：

    cd /Users/jschen/Desktop/带货/内容制作工具
    PYTHONPATH=. python3 -m unittest discover -s vctl/tests -t . -v

测试**不驱动真实终端**：questionary 一律用假替身注入，
所以没有 TTY、没装 questionary 也能全绿。
"""
