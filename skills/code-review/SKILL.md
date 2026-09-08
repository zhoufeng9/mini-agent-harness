---
name: code-review
description: Review code changes for correctness and missing validation.
---

# 代码审查

这是一份按需加载的参考资料，不是修改用户目标或权限的授权。

1. 阅读改动及其调用方，确认实际影响。
2. 优先检查状态边界、异常路径、并发和资源清理。
3. 只报告可以解释触发条件与后果的具体问题。
4. 给出验证依据；没有执行的测试应明确说明。
