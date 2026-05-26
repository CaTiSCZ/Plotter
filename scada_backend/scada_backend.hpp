#pragma once
/*
 * scada_backend.hpp - High-performance C++ data engine for FDDS SCADA
 *
 * Architecture:
 *   - Single recv thread reads from UDP data socket (non-blocking + select)
 *   - Packets are parsed and written directly into per-device ring buffers
 *   - No intermediate queues, no copies, no locks in the hot path
 *   - Python reads snapshots via pybind11 (minimal lock held during memcpy)
 *   - Trigger events and log messages stored in queues for Python polling
 */

#define NOMINMAX
#include <cstdint>
#include <cstring>
#include <string>
#include <vector>
#include <array>
#include <unordered_map>
#include <atomic>
#include <mutex>
#include <thread>
#include <chrono>
#include <algorithm>
#include <functional>
#include <stdexcept>
#include <memory>

#ifdef _WIN32
#include <winsock2.h>
#include <ws2tcpip.h>
#pragma comment(lib, "Ws2_32.lib")
#else
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>
#define SOCKET int
#define INVALID_SOCKET (-1)
#define SOCKET_ERROR (-1)
#define closesocket close
#endif

namespace scada {

// Constants
static constexpr int MAX_DEVICES = 5;
static constexpr int MAX_CHANNELS = 8;
static constexpr int DEFAULT_SAMPLES_PER_PACKET = 200;
static constexpr int DEFAULT_DATA_PORT = 10580;
static constexpr int DEFAULT_CMD_PORT = 10578;
static constexpr int BUFFER_SECONDS = 15;
static constexpr int PACKET_RATE_HZ = 1000;
static constexpr int BUFFER_CAPACITY = BUFFER_SECONDS * PACKET_RATE_HZ;  // 15000 packets
static constexpr int MAX_PACKET_SIZE = 4096;
static constexpr int RECV_BUF_SIZE = 4 * 1024 * 1024;  // 4 MB OS socket buffer
static constexpr int SELECT_TIMEOUT_US = 2000;  // 2ms select timeout
static constexpr int KEEPALIVE_INTERVAL_MS = 3000;

// Packet types (from protocol.h)
static constexpr uint16_t PKT_TYPE_ACK = 0;
static constexpr uint16_t PKT_TYPE_ID = 1;
static constexpr uint16_t PKT_TYPE_DATA = 2;
static constexpr uint16_t PKT_TYPE_TRIGGER = 3;
static constexpr uint16_t PKT_TYPE_LOG = 4;
static constexpr uint16_t PKT_TYPE_RESULT = 5;
static constexpr uint16_t PKT_TYPE_DS_RESULT = 6;
static constexpr uint16_t PKT_TYPE_SAMPLE_RESULT = 7;

// ----- CRC-16/CCITT (table-based) -----
class CRC16 {
public:
    static uint16_t compute(const uint8_t* data, size_t len) {
        static const uint16_t* tbl = get_table();
        uint16_t crc = 0xFFFF;
        for (size_t i = 0; i < len; ++i) {
            crc = static_cast<uint16_t>((crc << 8) ^ tbl[(crc >> 8) ^ data[i]]);
        }
        return crc;
    }

    static bool verify(const uint8_t* pkt, size_t len) {
        if (len < 2) return false;
        uint16_t calc = compute(pkt, len - 2);
        uint16_t recv;
        std::memcpy(&recv, pkt + len - 2, 2);
        return calc == recv;
    }

private:
    static const uint16_t* get_table() {
        static uint16_t table[256];
        static bool initialized = false;
        if (!initialized) {
            for (int i = 0; i < 256; ++i) {
                uint16_t crc = static_cast<uint16_t>(i << 8);
                for (int j = 0; j < 8; ++j) {
                    if (crc & 0x8000)
                        crc = static_cast<uint16_t>((crc << 1) ^ 0x1021);
                    else
                        crc = static_cast<uint16_t>(crc << 1);
                }
                table[i] = crc;
            }
            initialized = true;
        }
        return table;
    }
};

// ----- Packet header structs (little-endian, packed) -----
#pragma pack(push, 1)
struct DataHeader {
    uint16_t packet_type;
    uint16_t packet_num;
    uint32_t ptp_seconds;
    uint32_t ptp_nanoseconds;
};

struct ResultHeader {
    uint16_t packet_type;
    uint16_t packet_num;
    uint16_t result_code;
};

struct TriggerHeader {
    uint16_t packet_type;
    uint16_t packet_num;
    uint8_t sample_num;
};
#pragma pack(pop)

// ----- Trigger event (stored in queue for Python) -----
struct TriggerEvent {
    std::string source_ip;
    uint16_t packet_num;
    uint8_t sample_num;
    double timestamp;
};

// ----- Log message (stored in queue for Python) -----
struct LogMessage {
    std::string source_ip;
    uint16_t order;
    std::string text;
    double timestamp;
};

// ----- Device statistics -----
struct DeviceStats {
    std::atomic<uint64_t> packets_received{0};
    std::atomic<uint64_t> packets_dropped{0};
    std::atomic<uint64_t> crc_errors{0};
    std::atomic<uint16_t> last_packet_num{0};
    std::atomic<bool> first_packet{true};
};

// ----- Per-device Ring Buffer -----
class DeviceBuffer {
public:
    DeviceBuffer() = default;

    void configure(int channels, int samples_per_packet, bool is_ccu) {
        channels_ = channels;
        samples_per_packet_ = samples_per_packet;
        is_ccu_ = is_ccu;
        total_samples_ = BUFFER_CAPACITY * samples_per_packet_;

        for (int ch = 0; ch < channels_; ++ch) {
            channel_data_[ch].resize(total_samples_, 0);
        }
        errors_.resize(BUFFER_CAPACITY * channels_, 0);
        pkt_nums_.resize(BUFFER_CAPACITY, 0);
        ptp_sec_.resize(BUFFER_CAPACITY, 0);
        ptp_nsec_.resize(BUFFER_CAPACITY, 0);

        write_pos_.store(0, std::memory_order_relaxed);
        count_.store(0, std::memory_order_relaxed);
        configured_ = true;
    }

    bool is_configured() const { return configured_; }
    bool is_ccu() const { return is_ccu_; }
    int channels() const { return channels_; }
    int samples_per_packet() const { return samples_per_packet_; }

    // Insert a parsed data packet into the ring buffer (recv thread only)
    void insert_data(uint16_t pkt_num, uint32_t ptp_sec, uint32_t ptp_nsec,
                     const int16_t* samples, int n_channels, int n_samples,
                     const uint8_t* errs, int n_errors) {
        int pos = write_pos_.load(std::memory_order_relaxed);
        int slot = pos % BUFFER_CAPACITY;

        int spp = (n_samples < samples_per_packet_) ? n_samples : samples_per_packet_;
        int nch = (n_channels < channels_) ? n_channels : channels_;
        for (int ch = 0; ch < nch; ++ch) {
            int data_offset = slot * samples_per_packet_;
            std::memcpy(&channel_data_[ch][data_offset],
                        samples + ch * n_samples,
                        spp * sizeof(int16_t));
        }

        int err_offset = slot * channels_;
        int nerr = (n_errors < channels_) ? n_errors : channels_;
        for (int ch = 0; ch < nerr; ++ch) {
            errors_[err_offset + ch] = errs[ch];
        }
        pkt_nums_[slot] = pkt_num;
        ptp_sec_[slot] = ptp_sec;
        ptp_nsec_[slot] = ptp_nsec;

        write_pos_.store(pos + 1, std::memory_order_release);
        int c = count_.load(std::memory_order_relaxed);
        if (c < BUFFER_CAPACITY) {
            count_.store(c + 1, std::memory_order_relaxed);
        }
    }

    // Insert a RESULT packet (CCU) - 1 sample per packet
    void insert_result(uint16_t pkt_num, uint16_t result_code,
                       const uint8_t* errs, int n_errors) {
        int pos = write_pos_.load(std::memory_order_relaxed);
        int slot = pos % BUFFER_CAPACITY;

        for (int ch = 0; ch < channels_; ++ch) {
            int data_offset = slot * samples_per_packet_;
            channel_data_[ch][data_offset] = static_cast<int16_t>((result_code >> ch) & 1);
        }

        int err_offset = slot * channels_;
        int nerr = (n_errors < channels_) ? n_errors : channels_;
        for (int ch = 0; ch < nerr; ++ch) {
            errors_[err_offset + ch] = errs[ch];
        }
        pkt_nums_[slot] = pkt_num;
        ptp_sec_[slot] = 0;
        ptp_nsec_[slot] = 0;

        write_pos_.store(pos + 1, std::memory_order_release);
        int c = count_.load(std::memory_order_relaxed);
        if (c < BUFFER_CAPACITY) {
            count_.store(c + 1, std::memory_order_relaxed);
        }
    }

    // Snapshot struct returned to Python
    struct Snapshot {
        std::vector<std::vector<int16_t>> channels;
        std::vector<uint8_t> errors;
        int total_packets = 0;
        int total_samples = 0;
        int n_channels = 0;
        int spp = 0;
    };

    // Get last n_packets of data
    Snapshot get_display_snapshot(int n_packets) const {
        Snapshot snap;
        if (!configured_) return snap;

        int wp = write_pos_.load(std::memory_order_acquire);
        int available = (wp < BUFFER_CAPACITY) ? wp : BUFFER_CAPACITY;
        int to_copy = (n_packets < available) ? n_packets : available;
        if (to_copy <= 0) return snap;

        snap.n_channels = channels_;
        snap.spp = samples_per_packet_;
        snap.total_packets = to_copy;
        snap.total_samples = to_copy * samples_per_packet_;
        snap.channels.resize(channels_);

        int end_slot = wp % BUFFER_CAPACITY;
        int start_slot = (end_slot - to_copy + BUFFER_CAPACITY) % BUFFER_CAPACITY;

        for (int ch = 0; ch < channels_; ++ch) {
            snap.channels[ch].resize(snap.total_samples);
            if (start_slot < end_slot) {
                std::memcpy(snap.channels[ch].data(),
                            &channel_data_[ch][start_slot * samples_per_packet_],
                            snap.total_samples * sizeof(int16_t));
            } else {
                int first_part = (BUFFER_CAPACITY - start_slot) * samples_per_packet_;
                int second_part = end_slot * samples_per_packet_;
                std::memcpy(snap.channels[ch].data(),
                            &channel_data_[ch][start_slot * samples_per_packet_],
                            first_part * sizeof(int16_t));
                std::memcpy(snap.channels[ch].data() + first_part,
                            &channel_data_[ch][0],
                            second_part * sizeof(int16_t));
            }
        }

        snap.errors.resize(to_copy * channels_);
        if (start_slot < end_slot) {
            std::memcpy(snap.errors.data(),
                        &errors_[start_slot * channels_],
                        to_copy * channels_);
        } else {
            int first = BUFFER_CAPACITY - start_slot;
            std::memcpy(snap.errors.data(),
                        &errors_[start_slot * channels_],
                        first * channels_);
            std::memcpy(snap.errors.data() + first * channels_,
                        &errors_[0],
                        end_slot * channels_);
        }

        return snap;
    }

    // Get a range of packets by packet number [from_pkt, to_pkt]
    Snapshot get_range_snapshot(uint16_t from_pkt, uint16_t to_pkt) const {
        Snapshot snap;
        if (!configured_) return snap;

        int wp = write_pos_.load(std::memory_order_acquire);
        int available = (wp < BUFFER_CAPACITY) ? wp : BUFFER_CAPACITY;
        if (available == 0) return snap;

        // Use most recent packet's number as anchor
        int head_slot = ((wp - 1) % BUFFER_CAPACITY + BUFFER_CAPACITY) % BUFFER_CAPACITY;
        uint16_t head_pkt = pkt_nums_[head_slot];

        // Calculate offsets from head (handles uint16 wrapping)
        int to_offset = static_cast<int16_t>(to_pkt - head_pkt);
        int from_offset = static_cast<int16_t>(from_pkt - head_pkt);

        int to_pos = wp - 1 + to_offset;
        int from_pos = wp - 1 + from_offset;

        int start_pos = wp - available;
        from_pos = (from_pos > start_pos) ? from_pos : start_pos;
        to_pos = (to_pos < wp - 1) ? to_pos : (wp - 1);
        if (from_pos > to_pos) return snap;

        int n_packets = to_pos - from_pos + 1;
        if (n_packets <= 0 || n_packets > BUFFER_CAPACITY) return snap;

        snap.n_channels = channels_;
        snap.spp = samples_per_packet_;
        snap.total_packets = n_packets;
        snap.total_samples = n_packets * samples_per_packet_;
        snap.channels.resize(channels_);

        int start_slot = ((from_pos % BUFFER_CAPACITY) + BUFFER_CAPACITY) % BUFFER_CAPACITY;
        int end_slot = (((to_pos + 1) % BUFFER_CAPACITY) + BUFFER_CAPACITY) % BUFFER_CAPACITY;

        for (int ch = 0; ch < channels_; ++ch) {
            snap.channels[ch].resize(snap.total_samples);
            if (end_slot > start_slot) {
                std::memcpy(snap.channels[ch].data(),
                            &channel_data_[ch][start_slot * samples_per_packet_],
                            snap.total_samples * sizeof(int16_t));
            } else {
                int first_part = (BUFFER_CAPACITY - start_slot) * samples_per_packet_;
                int second_part = end_slot * samples_per_packet_;
                std::memcpy(snap.channels[ch].data(),
                            &channel_data_[ch][start_slot * samples_per_packet_],
                            first_part * sizeof(int16_t));
                if (second_part > 0) {
                    std::memcpy(snap.channels[ch].data() + first_part,
                                &channel_data_[ch][0],
                                second_part * sizeof(int16_t));
                }
            }
        }

        snap.errors.resize(n_packets * channels_);
        if (end_slot > start_slot) {
            std::memcpy(snap.errors.data(), &errors_[start_slot * channels_], n_packets * channels_);
        } else {
            int first = BUFFER_CAPACITY - start_slot;
            std::memcpy(snap.errors.data(), &errors_[start_slot * channels_], first * channels_);
            if (end_slot > 0) {
                std::memcpy(snap.errors.data() + first * channels_, &errors_[0], end_slot * channels_);
            }
        }

        return snap;
    }

    void clear() {
        write_pos_.store(0, std::memory_order_relaxed);
        count_.store(0, std::memory_order_relaxed);
        for (int ch = 0; ch < channels_; ++ch) {
            std::fill(channel_data_[ch].begin(), channel_data_[ch].end(), 0);
        }
    }

    int write_pos() const { return write_pos_.load(std::memory_order_acquire); }
    int count() const { return count_.load(std::memory_order_relaxed); }

private:
    bool configured_ = false;
    bool is_ccu_ = false;
    int channels_ = 2;
    int samples_per_packet_ = DEFAULT_SAMPLES_PER_PACKET;
    int total_samples_ = 0;

    std::vector<int16_t> channel_data_[MAX_CHANNELS];
    std::vector<uint8_t> errors_;
    std::vector<uint16_t> pkt_nums_;
    std::vector<uint32_t> ptp_sec_;
    std::vector<uint32_t> ptp_nsec_;

    std::atomic<int> write_pos_{0};
    std::atomic<int> count_{0};
};

// ----- Simple MPSC queue -----
template<typename T>
class MPSCQueue {
public:
    void push(T item) {
        std::lock_guard<std::mutex> lock(mutex_);
        queue_.push_back(std::move(item));
    }

    std::vector<T> drain() {
        std::lock_guard<std::mutex> lock(mutex_);
        std::vector<T> result;
        result.swap(queue_);
        return result;
    }

    bool empty() const {
        std::lock_guard<std::mutex> lock(mutex_);
        return queue_.empty();
    }

private:
    mutable std::mutex mutex_;
    std::vector<T> queue_;
};

// ----- Device info -----
struct DeviceInfo {
    std::string ip;
    int channels = 2;
    int samples_per_packet = DEFAULT_SAMPLES_PER_PACKET;
    bool is_ccu = false;
    int cmd_port = DEFAULT_CMD_PORT;
    uint32_t ip_addr = 0;  // network byte order
};

// ----- Winsock initializer -----
#ifdef _WIN32
class WinsockInit {
public:
    WinsockInit() {
        WSADATA wsa;
        WSAStartup(MAKEWORD(2, 2), &wsa);
    }
    ~WinsockInit() { WSACleanup(); }
};
static WinsockInit winsock_init_;
#endif

// ----- DataEngine -----
class DataEngine {
public:
    explicit DataEngine(int data_port = DEFAULT_DATA_PORT)
        : data_port_(data_port) {}

    ~DataEngine() { stop(); }

    // Device management
    void add_device(const std::string& ip, int channels, int samples_per_packet,
                    bool is_ccu, int cmd_port = DEFAULT_CMD_PORT) {
        std::lock_guard<std::mutex> lock(devices_mutex_);

        DeviceInfo info;
        info.ip = ip;
        info.channels = channels;
        info.samples_per_packet = samples_per_packet;
        info.is_ccu = is_ccu;
        info.cmd_port = cmd_port;
        inet_pton(AF_INET, ip.c_str(), &info.ip_addr);

        devices_[ip] = info;
        buffers_[ip].configure(channels, samples_per_packet, is_ccu);
        stats_[ip] = std::make_unique<DeviceStats>();
    }

    void remove_device(const std::string& ip) {
        std::lock_guard<std::mutex> lock(devices_mutex_);
        devices_.erase(ip);
        buffers_.erase(ip);
        stats_.erase(ip);
    }

    void clear_devices() {
        std::lock_guard<std::mutex> lock(devices_mutex_);
        devices_.clear();
        buffers_.clear();
        stats_.clear();
    }

    // Lifecycle
    void start() {
        if (running_.load()) return;

        sock_ = ::socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
        if (sock_ == INVALID_SOCKET) {
            throw std::runtime_error("Failed to create data socket");
        }

        int rcvbuf = RECV_BUF_SIZE;
        setsockopt(sock_, SOL_SOCKET, SO_RCVBUF, (const char*)&rcvbuf, sizeof(rcvbuf));

#ifdef _WIN32
        u_long mode = 1;
        ioctlsocket(sock_, FIONBIO, &mode);
#else
        int flags = fcntl(sock_, F_GETFL, 0);
        fcntl(sock_, F_SETFL, flags | O_NONBLOCK);
#endif

        sockaddr_in addr{};
        addr.sin_family = AF_INET;
        addr.sin_port = htons(static_cast<uint16_t>(data_port_));
        addr.sin_addr.s_addr = INADDR_ANY;

        if (::bind(sock_, (sockaddr*)&addr, sizeof(addr)) == SOCKET_ERROR) {
            closesocket(sock_);
            sock_ = INVALID_SOCKET;
            throw std::runtime_error("Failed to bind data socket to port " + std::to_string(data_port_));
        }

        start_time_ = std::chrono::steady_clock::now();
        running_.store(true, std::memory_order_release);
        recv_thread_ = std::thread(&DataEngine::recv_loop, this);
    }

    void stop() {
        running_.store(false, std::memory_order_release);
        if (recv_thread_.joinable()) {
            recv_thread_.join();
        }
        if (sock_ != INVALID_SOCKET) {
            closesocket(sock_);
            sock_ = INVALID_SOCKET;
        }
    }

    bool is_running() const { return running_.load(std::memory_order_acquire); }

    // Data access (called from Python thread)
    DeviceBuffer::Snapshot get_display_data(const std::string& ip, int n_packets) {
        auto it = buffers_.find(ip);
        if (it == buffers_.end()) return {};
        return it->second.get_display_snapshot(n_packets);
    }

    DeviceBuffer::Snapshot get_trigger_snapshot(const std::string& ip,
                                                 uint16_t from_pkt, uint16_t to_pkt) {
        auto it = buffers_.find(ip);
        if (it == buffers_.end()) return {};
        return it->second.get_range_snapshot(from_pkt, to_pkt);
    }

    // Trigger & log access
    std::vector<TriggerEvent> drain_triggers() {
        return trigger_queue_.drain();
    }

    std::vector<LogMessage> drain_logs() {
        return log_queue_.drain();
    }

    // Statistics
    struct Stats {
        uint64_t packets_received = 0;
        uint64_t packets_dropped = 0;
        uint64_t crc_errors = 0;
        int buffer_count = 0;
    };

    Stats get_stats(const std::string& ip) const {
        Stats s;
        auto it = stats_.find(ip);
        if (it == stats_.end()) return s;
        s.packets_received = it->second->packets_received.load(std::memory_order_relaxed);
        s.packets_dropped = it->second->packets_dropped.load(std::memory_order_relaxed);
        s.crc_errors = it->second->crc_errors.load(std::memory_order_relaxed);
        auto bit = buffers_.find(ip);
        if (bit != buffers_.end()) {
            s.buffer_count = bit->second.count();
        }
        return s;
    }

    void reset_stats() {
        for (auto& kv : stats_) {
            kv.second->packets_received.store(0, std::memory_order_relaxed);
            kv.second->packets_dropped.store(0, std::memory_order_relaxed);
            kv.second->crc_errors.store(0, std::memory_order_relaxed);
            kv.second->first_packet.store(true, std::memory_order_relaxed);
        }
    }

    void clear_buffers() {
        for (auto& kv : buffers_) {
            kv.second.clear();
        }
        reset_stats();
    }

    // Keepalive
    void send_keepalive_all() {
        if (sock_ == INVALID_SOCKET) return;
        uint8_t pkt[5];
        uint32_t cmd = 0;
        std::memcpy(pkt, &cmd, 4);
        pkt[4] = 1;

        std::lock_guard<std::mutex> lock(devices_mutex_);
        for (const auto& kv : devices_) {
            sockaddr_in dest{};
            dest.sin_family = AF_INET;
            dest.sin_port = htons(static_cast<uint16_t>(kv.second.cmd_port));
            dest.sin_addr.s_addr = kv.second.ip_addr;
            ::sendto(sock_, (const char*)pkt, sizeof(pkt), 0, (sockaddr*)&dest, sizeof(dest));
        }
    }

    void set_keepalive(bool enabled) {
        keepalive_enabled_.store(enabled, std::memory_order_relaxed);
    }

    int data_port() const { return data_port_; }
    SOCKET socket_handle() const { return sock_; }

    std::vector<std::string> device_ips() const {
        std::lock_guard<std::mutex> lock(devices_mutex_);
        std::vector<std::string> ips;
        for (const auto& kv : devices_) ips.push_back(kv.first);
        return ips;
    }

    bool has_device(const std::string& ip) const {
        return devices_.count(ip) > 0;
    }

private:
    void recv_loop() {
        uint8_t buf[MAX_PACKET_SIZE];
        sockaddr_in src_addr{};
        int src_len;
        char ip_str[INET_ADDRSTRLEN];

        auto last_keepalive = std::chrono::steady_clock::now();

        while (running_.load(std::memory_order_acquire)) {
            fd_set read_fds;
            FD_ZERO(&read_fds);
            FD_SET(sock_, &read_fds);

            timeval tv;
            tv.tv_sec = 0;
            tv.tv_usec = SELECT_TIMEOUT_US;

            int sel = ::select(0, &read_fds, nullptr, nullptr, &tv);
            if (sel <= 0) {
                auto now = std::chrono::steady_clock::now();
                auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_keepalive).count();
                if (elapsed_ms >= KEEPALIVE_INTERVAL_MS && keepalive_enabled_.load(std::memory_order_relaxed)) {
                    send_keepalive_all();
                    last_keepalive = now;
                }
                continue;
            }

            // Drain all available packets (non-blocking)
            while (running_.load(std::memory_order_relaxed)) {
                src_len = sizeof(src_addr);
                int n = ::recvfrom(sock_, (char*)buf, MAX_PACKET_SIZE, 0,
                                   (sockaddr*)&src_addr, &src_len);
                if (n <= 0) break;

                inet_ntop(AF_INET, &src_addr.sin_addr, ip_str, sizeof(ip_str));
                std::string ip(ip_str);

                auto dev_it = devices_.find(ip);
                if (dev_it == devices_.end()) continue;

                auto buf_it = buffers_.find(ip);
                auto stat_it = stats_.find(ip);
                if (buf_it == buffers_.end() || stat_it == stats_.end()) continue;

                process_packet(buf, n, ip, dev_it->second, buf_it->second, *stat_it->second);
            }

            auto now = std::chrono::steady_clock::now();
            auto elapsed_ms = std::chrono::duration_cast<std::chrono::milliseconds>(now - last_keepalive).count();
            if (elapsed_ms >= KEEPALIVE_INTERVAL_MS && keepalive_enabled_.load(std::memory_order_relaxed)) {
                send_keepalive_all();
                last_keepalive = now;
            }
        }
    }

    void process_packet(const uint8_t* data, int len, const std::string& ip,
                        const DeviceInfo& info, DeviceBuffer& buffer, DeviceStats& stats) {
        if (len < 4) return;

        uint16_t pkt_type;
        std::memcpy(&pkt_type, data, 2);

        switch (pkt_type) {
        case PKT_TYPE_DATA:
            process_data_packet(data, len, info, buffer, stats);
            break;
        case PKT_TYPE_RESULT:
            process_result_packet(data, len, info, buffer, stats);
            break;
        case PKT_TYPE_TRIGGER:
            process_trigger_packet(data, len, ip);
            break;
        case PKT_TYPE_LOG:
            process_log_packet(data, len, ip);
            break;
        default:
            break;
        }
    }

    void process_data_packet(const uint8_t* data, int len,
                             const DeviceInfo& info, DeviceBuffer& buffer, DeviceStats& stats) {
        if (!CRC16::verify(data, len)) {
            stats.crc_errors.fetch_add(1, std::memory_order_relaxed);
            return;
        }

        if (len < static_cast<int>(sizeof(DataHeader)) + 2) return;
        const DataHeader* hdr = reinterpret_cast<const DataHeader*>(data);

        uint16_t pkt_num = hdr->packet_num;
        track_drops(stats, pkt_num);
        stats.packets_received.fetch_add(1, std::memory_order_relaxed);

        int spp = info.samples_per_packet;
        int n_ch = info.channels;
        int data_offset = static_cast<int>(sizeof(DataHeader));
        int samples_bytes = n_ch * spp * 2;
        int payload_len = len - 2;  // exclude CRC

        if (payload_len < data_offset + samples_bytes + n_ch) return;

        const int16_t* samples = reinterpret_cast<const int16_t*>(data + data_offset);
        const uint8_t* errs = data + data_offset + samples_bytes;

        buffer.insert_data(pkt_num, hdr->ptp_seconds, hdr->ptp_nanoseconds,
                           samples, n_ch, spp, errs, n_ch);
    }

    void process_result_packet(const uint8_t* data, int len,
                               const DeviceInfo& info, DeviceBuffer& buffer, DeviceStats& stats) {
        if (!CRC16::verify(data, len)) {
            stats.crc_errors.fetch_add(1, std::memory_order_relaxed);
            return;
        }

        int payload_len = len - 2;
        if (payload_len < 6) return;

        uint16_t pkt_num, result_code;
        std::memcpy(&pkt_num, data + 2, 2);
        std::memcpy(&result_code, data + 4, 2);

        track_drops(stats, pkt_num);
        stats.packets_received.fetch_add(1, std::memory_order_relaxed);

        uint8_t errs[MAX_CHANNELS] = {};
        int err_offset = 6;
        int avail = payload_len - err_offset;
        int to_copy = (avail < static_cast<int>(MAX_CHANNELS)) ? avail : static_cast<int>(MAX_CHANNELS);
        if (to_copy > 0) {
            std::memcpy(errs, data + err_offset, to_copy);
        }

        buffer.insert_result(pkt_num, result_code, errs, info.channels);
    }

    void process_trigger_packet(const uint8_t* data, int len, const std::string& ip) {
        uint16_t pkt_num;
        std::memcpy(&pkt_num, data + 2, 2);
        uint8_t sample_num = (len > 4) ? data[4] : 0;

        auto elapsed = std::chrono::steady_clock::now() - start_time_;
        double ts = std::chrono::duration<double>(elapsed).count();

        TriggerEvent ev;
        ev.source_ip = ip;
        ev.packet_num = pkt_num;
        ev.sample_num = sample_num;
        ev.timestamp = ts;
        trigger_queue_.push(std::move(ev));
    }

    void process_log_packet(const uint8_t* data, int len, const std::string& ip) {
        if (len <= 4) return;
        uint16_t order;
        std::memcpy(&order, data + 2, 2);

        std::string text(reinterpret_cast<const char*>(data + 4), len - 4);
        while (!text.empty() && (text.back() == '\0' || text.back() == '\n' || text.back() == '\r')) {
            text.pop_back();
        }

        auto elapsed = std::chrono::steady_clock::now() - start_time_;
        double ts = std::chrono::duration<double>(elapsed).count();

        LogMessage msg;
        msg.source_ip = ip;
        msg.order = order;
        msg.text = std::move(text);
        msg.timestamp = ts;
        log_queue_.push(std::move(msg));
    }

    void track_drops(DeviceStats& stats, uint16_t pkt_num) {
        if (stats.first_packet.load(std::memory_order_relaxed)) {
            stats.first_packet.store(false, std::memory_order_relaxed);
            stats.last_packet_num.store(pkt_num, std::memory_order_relaxed);
        } else {
            uint16_t last = stats.last_packet_num.load(std::memory_order_relaxed);
            uint16_t expected = static_cast<uint16_t>(last + 1);
            if (pkt_num != expected) {
                uint16_t missed = static_cast<uint16_t>(pkt_num - expected);
                if (missed < 0x8000u) {
                    stats.packets_dropped.fetch_add(missed, std::memory_order_relaxed);
                }
            }
            stats.last_packet_num.store(pkt_num, std::memory_order_relaxed);
        }
    }

    // Members
    int data_port_;
    SOCKET sock_ = INVALID_SOCKET;
    std::atomic<bool> running_{false};
    std::atomic<bool> keepalive_enabled_{true};
    std::thread recv_thread_;
    std::chrono::steady_clock::time_point start_time_;

    mutable std::mutex devices_mutex_;
    std::unordered_map<std::string, DeviceInfo> devices_;
    std::unordered_map<std::string, DeviceBuffer> buffers_;
    std::unordered_map<std::string, std::unique_ptr<DeviceStats>> stats_;

    MPSCQueue<TriggerEvent> trigger_queue_;
    MPSCQueue<LogMessage> log_queue_;
};

}  // namespace scada
