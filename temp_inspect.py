import traceback
import pyrogram.sync
orig = pyrogram.sync.async_to_sync

def debug_async_to_sync(obj, name):
    print('async_to_sync called with', obj, name)
    return orig(obj, name)

pyrogram.sync.async_to_sync = debug_async_to_sync

try:
    import kurimod
except Exception as e:
    traceback.print_exc()
