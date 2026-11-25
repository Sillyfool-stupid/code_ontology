#include "plant.h"

namespace mini_plant {

int runDemonstration() {
    // These scenarios are simulated code examples, not real plant events.
    Pump coolingPump(101, defaultLimits());
    if (!coolingPump.start()) {
        return 1;
    }

    coolingPump.update({60.0, 210.0, 2.0, true});
    coolingPump.tickHours(10);
    if (coolingPump.state() != DeviceState::Running) {
        return 2;
    }

    coolingPump.update({90.0, 210.0, 2.0, true});
    if (coolingPump.alarm().code != AlarmCode::OverTemperature) {
        return 3;
    }
    coolingPump.acknowledgeAlarm();
    if (coolingPump.resetFault()) {
        return 4;
    }
    coolingPump.update({60.0, 210.0, 2.0, true});
    if (!coolingPump.resetFault()) {
        return 5;
    }

    Pump feedPump(102, defaultLimits());
    feedPump.start();
    feedPump.tickHours(500);
    if (feedPump.alarm().code != AlarmCode::ServiceDue) {
        return 6;
    }
    feedPump.acknowledgeAlarm();
    if (!feedPump.scheduleMaintenance()) {
        return 7;
    }
    if (!feedPump.completeMaintenance()) {
        return 8;
    }

    Pump reservePump(103, defaultLimits());
    reservePump.start();
    reservePump.update({50.0, 220.0, 1.0, false});
    if (reservePump.alarm().code != AlarmCode::SensorOffline) {
        return 9;
    }
    reservePump.acknowledgeAlarm();
    reservePump.update({50.0, 220.0, 1.0, true});
    if (!reservePump.resetFault()) {
        return 10;
    }

    return 0;
}

}  // namespace mini_plant

int main() {
    return mini_plant::runDemonstration();
}
