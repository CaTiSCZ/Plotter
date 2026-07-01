// cppimport
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "buffered_socket.hpp"
#include <tuple>

namespace py = pybind11;

using BufferedSocket_Container = std::vector<uint8_t>;
using BufferedSocket = buffered_socket::BufferedSocket<BufferedSocket_Container>;

PYBIND11_MODULE(buffered_socket_cpp, m) {
    py::object py_socket = py::module_::import("socket");
    py::object py_socket_timeout = py_socket.attr("timeout");
    py::register_exception<buffered_socket::SocketTimeout>(m, "SocketTimeout", py_socket_timeout.ptr());
    py::class_<BufferedSocket>(m, "BufferedSocket")
        .def(py::init<int, const std::string&>(), py::arg("max_size") = 4096, py::arg("name") = "BufferedSocket")
        .def("bind", [](BufferedSocket& self, int port, bool use_my_ip, const std::string& device_ip, int device_port) {
            py::object socket_mod = py::module_::import("socket");

            std::string local_ip = "0.0.0.0";
            if (use_my_ip) {
                py::object tmp = socket_mod.attr("socket")(
                    socket_mod.attr("AF_INET"), socket_mod.attr("SOCK_DGRAM"));
                tmp.attr("connect")(py::make_tuple(device_ip, device_port));
                py::tuple name = tmp.attr("getsockname")();
                local_ip = name[0].cast<std::string>();
                tmp.attr("close")();
            }

            py::object sock = socket_mod.attr("socket")(
                socket_mod.attr("AF_INET"), socket_mod.attr("SOCK_DGRAM"));
            sock.attr("bind")(py::make_tuple(local_ip, port));

            auto fd = static_cast<SOCKET>(sock.attr("detach")().cast<size_t>());
            self.attach(fd);

            return py::make_tuple(local_ip, port);
        }, py::arg("port"), py::arg("use_my_ip")=false, py::arg("device_ip")="192.168.1.100", py::arg("device_port")=9999)
        .def("close", &BufferedSocket::close)
        .def("sendto", [](BufferedSocket& self, py::bytes data, py::tuple addr) {
            if (addr.size() != 2)
                throw std::runtime_error("Address must be a tuple (ip, port)");
            std::string ip = py::str(addr[0]);
            int port = py::int_(addr[1]);
            const char* pdata = PyBytes_AsString(data.ptr());
            size_t size = PyBytes_Size(data.ptr());
            self.sendto(std::vector<uint8_t>(pdata, pdata + size), ip, port);
        }, py::arg("data"), py::arg("address"))
        .def("recvfrom", [](BufferedSocket& self, int bufsize) {
            std::pair<BufferedSocket_Container, std::pair<std::string, int>> result;
            {
                py::gil_scoped_release release;
                result = std::move(self.recvfrom(bufsize));
                if (result.first.size() == 0)
                    throw std::runtime_error("Empty packet received");
                if (result.first.data() == nullptr)
                    throw std::runtime_error("null data");
            }
            return py::make_tuple(py::bytes(reinterpret_cast<const char*>(result.first.data()), result.first.size()),
                                  py::make_tuple(result.second.first, result.second.second));
        })
        .def("settimeout", &BufferedSocket::settimeout)
        .def("set_recv_buffer", &BufferedSocket::set_recv_buffer, py::arg("bytes"))
        .def("set_listener_priority", &BufferedSocket::set_listener_priority, py::arg("priority"))
        .def("get_received_count", &BufferedSocket::get_received_count)
        .def("get_buffered_items_count", &BufferedSocket::get_buffered_items_count)
        .def("__bool__", [](const BufferedSocket& self) {
            return self.sock_ != INVALID_SOCKET && self.running_;
        });

    // Windows thread priority constants for use with set_listener_priority().
    m.attr("THREAD_PRIORITY_IDLE") = (int)THREAD_PRIORITY_IDLE;
    m.attr("THREAD_PRIORITY_LOWEST") = (int)THREAD_PRIORITY_LOWEST;
    m.attr("THREAD_PRIORITY_BELOW_NORMAL") = (int)THREAD_PRIORITY_BELOW_NORMAL;
    m.attr("THREAD_PRIORITY_NORMAL") = (int)THREAD_PRIORITY_NORMAL;
    m.attr("THREAD_PRIORITY_ABOVE_NORMAL") = (int)THREAD_PRIORITY_ABOVE_NORMAL;
    m.attr("THREAD_PRIORITY_HIGHEST") = (int)THREAD_PRIORITY_HIGHEST;
    m.attr("THREAD_PRIORITY_TIME_CRITICAL") = (int)THREAD_PRIORITY_TIME_CRITICAL;
}

/*
<%
cfg["dependencies"] = ["buffered_socket.hpp"]

# Debug build:
# cfg["extra_compile_args"] = ["/MT", "/Z7", "/Od", "/Ob0", "/Oy-"]
# cfg["extra_link_args"] = ["/NODEFAULTLIB:msvcrt.lib", "/DEBUG:FULL", "/INCREMENTAL:NO", "/PDB:buffered_socket_cpp.pdb"]

# Release build (optimized, no debug symbols):
cfg["extra_compile_args"] = ["/MT", "/O2", "/GL", "/DNDEBUG"]
cfg["extra_link_args"] = ["/NODEFAULTLIB:msvcrt.lib", "/LTCG", "/INCREMENTAL:NO"]

setup_pybind11(cfg)
%>
*/
