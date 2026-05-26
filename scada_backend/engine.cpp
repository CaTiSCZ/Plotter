// cppimport
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include <pybind11/numpy.h>
#include "scada_backend.hpp"

namespace py = pybind11;
using namespace scada;

PYBIND11_MODULE(engine, m) {
    m.doc() = "FDDS SCADA C++ data engine";

    // TriggerEvent
    py::class_<TriggerEvent>(m, "TriggerEvent")
        .def_readonly("source_ip", &TriggerEvent::source_ip)
        .def_readonly("packet_num", &TriggerEvent::packet_num)
        .def_readonly("sample_num", &TriggerEvent::sample_num)
        .def_readonly("timestamp", &TriggerEvent::timestamp);

    // LogMessage
    py::class_<LogMessage>(m, "LogMessage")
        .def_readonly("source_ip", &LogMessage::source_ip)
        .def_readonly("order", &LogMessage::order)
        .def_readonly("text", &LogMessage::text)
        .def_readonly("timestamp", &LogMessage::timestamp);

    // Stats
    py::class_<DataEngine::Stats>(m, "Stats")
        .def_readonly("packets_received", &DataEngine::Stats::packets_received)
        .def_readonly("packets_dropped", &DataEngine::Stats::packets_dropped)
        .def_readonly("crc_errors", &DataEngine::Stats::crc_errors)
        .def_readonly("buffer_count", &DataEngine::Stats::buffer_count);

    // Helper lambda: Snapshot -> Python dict with numpy arrays
    auto snapshot_to_dict = [](const DeviceBuffer::Snapshot& snap) -> py::dict {
        py::dict d;
        if (snap.total_packets == 0) {
            d["empty"] = true;
            return d;
        }
        d["empty"] = false;
        d["total_packets"] = snap.total_packets;
        d["total_samples"] = snap.total_samples;
        d["n_channels"] = snap.n_channels;
        d["spp"] = snap.spp;

        py::array_t<int16_t> data({snap.n_channels, snap.total_samples});
        auto buf = data.mutable_unchecked<2>();
        for (int ch = 0; ch < snap.n_channels; ++ch) {
            std::memcpy(&buf(ch, 0), snap.channels[ch].data(),
                        snap.total_samples * sizeof(int16_t));
        }
        d["data"] = data;

        py::array_t<uint8_t> errors({snap.total_packets, snap.n_channels});
        auto ebuf = errors.mutable_unchecked<2>();
        std::memcpy(ebuf.mutable_data(0, 0), snap.errors.data(),
                    snap.total_packets * snap.n_channels);
        d["errors"] = errors;

        return d;
    };

    // DataEngine
    py::class_<DataEngine>(m, "DataEngine")
        .def(py::init<int>(), py::arg("data_port") = DEFAULT_DATA_PORT)

        .def("add_device", &DataEngine::add_device,
             py::arg("ip"), py::arg("channels") = 2,
             py::arg("samples_per_packet") = DEFAULT_SAMPLES_PER_PACKET,
             py::arg("is_ccu") = false, py::arg("cmd_port") = DEFAULT_CMD_PORT)
        .def("remove_device", &DataEngine::remove_device)
        .def("clear_devices", &DataEngine::clear_devices)
        .def("has_device", &DataEngine::has_device)
        .def("device_ips", &DataEngine::device_ips)

        .def("start", &DataEngine::start)
        .def("stop", &DataEngine::stop)
        .def("is_running", &DataEngine::is_running)

        .def("get_display_data", [snapshot_to_dict](DataEngine& self, const std::string& ip, int n_packets) {
            py::gil_scoped_release release;
            auto snap = self.get_display_data(ip, n_packets);
            py::gil_scoped_acquire acquire;
            return snapshot_to_dict(snap);
        }, py::arg("ip"), py::arg("n_packets"))

        .def("get_trigger_snapshot", [snapshot_to_dict](DataEngine& self, const std::string& ip,
                                                         uint16_t from_pkt, uint16_t to_pkt) {
            py::gil_scoped_release release;
            auto snap = self.get_trigger_snapshot(ip, from_pkt, to_pkt);
            py::gil_scoped_acquire acquire;
            return snapshot_to_dict(snap);
        }, py::arg("ip"), py::arg("from_pkt"), py::arg("to_pkt"))

        .def("drain_triggers", &DataEngine::drain_triggers)
        .def("drain_logs", &DataEngine::drain_logs)

        .def("get_stats", &DataEngine::get_stats)
        .def("reset_stats", &DataEngine::reset_stats)
        .def("clear_buffers", &DataEngine::clear_buffers)

        .def("send_keepalive_all", [](DataEngine& self) {
            py::gil_scoped_release release;
            self.send_keepalive_all();
        })
        .def("set_keepalive", &DataEngine::set_keepalive)

        .def_property_readonly("data_port", &DataEngine::data_port);

    // Module constants
    m.attr("MAX_DEVICES") = MAX_DEVICES;
    m.attr("MAX_CHANNELS") = MAX_CHANNELS;
    m.attr("BUFFER_CAPACITY") = BUFFER_CAPACITY;
    m.attr("BUFFER_SECONDS") = BUFFER_SECONDS;
    m.attr("PACKET_RATE_HZ") = PACKET_RATE_HZ;
    m.attr("DEFAULT_DATA_PORT") = DEFAULT_DATA_PORT;
    m.attr("DEFAULT_CMD_PORT") = DEFAULT_CMD_PORT;
    m.attr("DEFAULT_SAMPLES_PER_PACKET") = DEFAULT_SAMPLES_PER_PACKET;
}

/*
<%
cfg["dependencies"] = ["scada_backend.hpp"]
cfg["extra_compile_args"] = ["/MT", "/O2", "/GL", "/DNDEBUG", "/EHsc", "/std:c++17"]
cfg["extra_link_args"] = ["/NODEFAULTLIB:msvcrt.lib", "/LTCG", "/INCREMENTAL:NO", "Ws2_32.lib"]
setup_pybind11(cfg)
%>
*/
