# AIPod Todo CLI (no LLM required)

This is a small, fully checked-in AIPod project. It demonstrates the runtime
without calling an LLM: two `service` components are registered in the Bean
Pool and composed into a pipeline.

From the repository root:

```bash
pip install -e .
cd examples/todo_cli
python main.py "Buy groceries"
```

Expected output includes `BUY GROCERIES` and two recorded pipeline steps.
