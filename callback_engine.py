from collections import deque
from threading import Thread
from functools import partial

class Callback_engine(object):
    @staticmethod
    def _async_proxy(callback, *args, **kwargs):
        Thread(target=callback, args=args, kwargs=kwargs, daemon=False).start()

    def __init__(self, default_async = False):
        self._events = {}
        if default_async:
            from worker_thread import Worker_thread
            self._async_worker = Worker_thread(target=lambda args: self._sync_call(args[0], *args[1], **args[2]),
                                               name=f'{self.__class__.__name__}.{__class__.__name__}.async_worker')
            self._async_worker.start()
            self._call = lambda event, *args, **kwargs: self._async_worker.put((event, args, kwargs))
            for attr in ('join', 'is_alive'):
                setattr(self, attr, getattr(self._async_worker, attr))
        else:
            self._call = self._sync_call

    # def __del__(self):
    #     if hasattr(self, 'join'):
    #         self.join()

    def _sync_call(self, event, *args, **kwargs):
        if event in self._events:
            return [fcn(*args, **kwargs) for fcn in self._events[event]]
        return []
    
    def _async_call(self, event, *args, **kwargs):
        if event in self._events:
            for fcn in self._events[event]:
                if isinstance(fcn, partial) and fcn.func is Callback_engine._async_call:
                    fcn(*args, **kwargs)
                else:
                    Thread(target=fcn, args=args, kwargs=kwargs, daemon=False).start()

    def bind(self, event, callback, *, call_asynchronously = False):
        if callback is None:
            if event in self._events:
                del self._events[event]
        else:
            if call_asynchronously:
                callback = partial(Callback_engine._async_proxy, callback)
            if event in self._events:
                self._events[event].append(callback)
            else:
                self._events[event] = deque((callback,))
            return callback
        
    def unbind(self, event, callback):
        self._events[event].remove(callback)

