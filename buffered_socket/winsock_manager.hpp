#pragma once
#include <winsock2.h>
#include <stdexcept>
#include <mutex>

class WinsockManager {
public:
    static void ensure_initialized() {
        std::lock_guard<std::mutex> lock(get_mutex());
        if (!initialized()) {
            WSADATA wsaData;
            if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
                throw std::runtime_error("WSAStartup failed");
            }
            initialized() = true;
            // Registrace úklidu při ukončení procesu
            std::atexit(&WinsockManager::cleanup);
        }
    }

private:
    static bool& initialized() {
        static bool flag = false;
        return flag;
    }
    static std::mutex& get_mutex() {
        static std::mutex m;
        return m;
    }
    static void cleanup() {
        std::lock_guard<std::mutex> lock(get_mutex());
        if (initialized()) {
            WSACleanup();
            initialized() = false;
        }
    }
};
