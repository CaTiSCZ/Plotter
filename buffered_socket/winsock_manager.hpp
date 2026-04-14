#pragma once
#include <winsock2.h>
#include <stdexcept>
#include <mutex>

class WinsockManager {
public:
    static void ensure_initialized() {
        std::call_once(get_once_flag(), []() {
            WSADATA wsaData;
            if (WSAStartup(MAKEWORD(2, 2), &wsaData) != 0) {
                throw std::runtime_error("WSAStartup failed");
            }
        });
    }

private:
    static std::once_flag& get_once_flag() {
        static std::once_flag flag;
        return flag;
    }
};
