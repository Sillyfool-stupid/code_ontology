#include "maintenance.h"
namespace plant {
MaintenanceTask MaintenancePlanner::plan(const Alarm& alarm) {
    if (alarm.fault == FaultCode::OverTemperature) {
        return {alarm.deviceId, "stop and inspect bearing", TaskStatus::Pending};
    }
    if (alarm.fault == FaultCode::SensorOffline) {
        return {alarm.deviceId, "replace or reconnect sensor", TaskStatus::Pending};
    }
    return {alarm.deviceId, "no maintenance required", TaskStatus::Complete};
}
void MaintenancePlanner::execute(MaintenanceTask& task, Device& device) {
    if (task.status == TaskStatus::Pending) {
        device.stop();
        task.status = TaskStatus::Complete;
    }
}
}
