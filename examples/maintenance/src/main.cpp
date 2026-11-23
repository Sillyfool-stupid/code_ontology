#include "maintenance.h"
int main() {
    // 示例程序构造的设备，不是已核实的现场资产。
    plant::Pump pump("DEMO-PUMP-01");
    plant::TemperatureMonitor monitor(85.0);
    plant::Reading reading{90.0, "C", true};
    plant::Alarm alarm = monitor.evaluate(pump, reading);
    plant::MaintenancePlanner planner;
    auto task = planner.plan(alarm);
    planner.execute(task, pump);
    return 0;
}
