#pragma once
#include "device.h"
#include "monitor.h"

namespace plant {
enum class TaskStatus { Pending, Complete };
struct MaintenanceTask {
    std::string deviceId;
    std::string action;
    TaskStatus status;
};

// 运维策略将故障码转为处置任务。
class MaintenancePlanner {
public:
    MaintenanceTask plan(const Alarm& alarm);
    void execute(MaintenanceTask& task, Device& device);
};
}
