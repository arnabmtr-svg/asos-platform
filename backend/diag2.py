"""diag2.py — find WHY kiteconnect import fails. Run from backend/"""
print("Trying: from kiteconnect import KiteConnect")
try:
    from kiteconnect import KiteConnect
    print("SUCCESS — KiteConnect imported fine")
    print("Version:", __import__('kiteconnect').__version__ if hasattr(__import__('kiteconnect'),'__version__') else '?')
except ImportError as e:
    print("IMPORT ERROR:", e)
except Exception as e:
    print("OTHER ERROR:", type(e).__name__, "-", str(e))
    import traceback
    traceback.print_exc()

print()
print("Python executable:", __import__('sys').executable)
print("Python version:", __import__('sys').version)
