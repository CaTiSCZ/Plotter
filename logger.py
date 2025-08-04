import logging, logging.handlers
import threading
from queue import Queue
from contextlib import AbstractContextManager

application_logger = None

logger = None

class PlainTextTCPHandler(logging.handlers.SocketHandler):
    def __init__(self, host: str, port: int | None) -> None:
        super().__init__(host, port)
    def makePickle(self, record):
        message = self.formatter.format(record)
        return message.encode()
    
class QueueHandler(logging.handlers.QueueHandler):
    def __init__(self, queue, /, utilization_viewer = None):
        self._queue = queue
        self._utilization_viewer_lock = threading.Lock()
        self._utilization_viewer = utilization_viewer
        super().__init__(queue)

    def set_utilization_viewer(self, utilization_viewer):
        with self._utilization_viewer_lock:
            print('QueueHandler set log to', id(utilization_viewer) if utilization_viewer is not None else 'None')
            self._utilization_viewer = utilization_viewer

    def enqueue(self, record):
        return super().enqueue(record)
        with self._utilization_viewer_lock:
            if self._utilization_viewer is not None:
                self._utilization_viewer(self.queue.qsize())
            return ret

class QueueListener(logging.handlers.QueueListener):
    def __init__(self, queue, *handlers, utilization_viewer = None, **kwargs):
        self._queue = queue
        self._utilization_viewer_lock = threading.Lock()
        self._utilization_viewer = utilization_viewer
        super().__init__(queue, *handlers, **kwargs)
        self.handlers = list(self.handlers)

    def set_utilization_viewer(self, utilization_viewer):
        with self._utilization_viewer_lock:
            print('QueueListener set log to', id(utilization_viewer) if utilization_viewer is not None else 'None')
            self._utilization_viewer = utilization_viewer

    def dequeue(self, block):
        return super().dequeue(block)
        with self._utilization_viewer_lock:
            if self._utilization_viewer is not None:
                self._utilization_viewer(self.queue.qsize())
            return res

class GuiHandler(logging.Handler):
    def __init__(self, sink, level = logging.DEBUG):
        super().__init__(level)
        self.sink = sink
    def emit(self, record):
        if self.sink is not None:
            self.sink(self.formatter.format(record))

class Logging(AbstractContextManager):
    def __init__(self, tcp_host='localhost', tcp_port=12344):
        super().__init__()
        self.logger = logging.getLogger(application_logger)
        self.logger.setLevel(logging.DEBUG) # nastavení úrovně vypisování hlášek - global
        self.logger_queue = Queue(-1)
        self.logger_queue_handler = QueueHandler(self.logger_queue)
        self.logger.addHandler(self.logger_queue_handler)
        self.log_console_handler = logging.StreamHandler()
        self.log_console_handler.setFormatter(logging.Formatter('%(asctime)s\t%(levelname)-8s\t%(threadName)-30s\t%(name)-50s\t%(message)s'))
        self.log_console_handler.setLevel(logging.DEBUG)  # nastavení úrovně vypisování hlášek - console
        self.log_tcp_handler = PlainTextTCPHandler(tcp_host, tcp_port)
        self.log_tcp_handler.setFormatter(logging.Formatter('%(asctime)s\t%(levelname)-8s\t%(threadName)-30s\t%(name)-50s\t%(message)s\n'))
        self.log_tcp_handler.setLevel(logging.DEBUG) # nastavení úrovně vypisování hlášek - TCP
        self.log_tcp_handler.createSocket()
        self.log_printer = QueueListener(self.logger_queue, self.log_console_handler, self.log_tcp_handler, respect_handler_level=True)
        self.log_printer.start()
        self.logger.info('Application start')
        global logger
        logger = self.logger
        
    def __exit__(self, exc_type, exc_value, traceback):
        self.logger.info('Application end')
        self.log_printer.stop()
        self.log_tcp_handler.close()
