# import logging
# import sys
# root_logger = logging.getLogger()
# root_logger.setLevel(logging.DEBUG)
# handler = logging.StreamHandler(sys.stdout)
# handler.setLevel(logging.DEBUG)
# formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
# handler.setFormatter(formatter)
# root_logger.addHandler(handler)

USE_PYTHON_SOCKET = False
if USE_PYTHON_SOCKET:
    from buffered_socket_py import BufferedSocket
else:
    import cppimport
    print("Importuji C++ BufferedSocket...")
    buffered_socket = cppimport.imp("buffered_socket.buffered_socket_cpp")
    BufferedSocket = buffered_socket.BufferedSocket

from buffered_socket_py import test_main

if __name__ == '__main__':
    test_main(BufferedSocket)
