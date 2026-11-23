#include "monitor.h"
void simulated_failure() {
    plant::Pump fake("TEST-PUMP");
    plant::TemperatureMonitor monitor(10.0);
    monitor.evaluate(fake, {1000.0, "C", true});
}
