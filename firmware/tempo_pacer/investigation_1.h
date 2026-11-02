// Opt-in development acquisition, compiled only with TEMPO_INVESTIGATION_1.
// A1/A2: raw, sequential ADC samples. No voltage conversion or output adjustment.
#pragma once

void reply(uint32_t id, const char *result, const char *error);
constexpr uint8_t SI1_CAPACITY = 64, SI1_PAGE = 6;
struct Si1Reading {
  uint32_t started, finished, cycleBefore, cycleAfter;
  uint16_t raw;
  uint8_t pin, modeBefore, modeAfter, outputBefore, outputAfter;
  bool highBefore, highAfter;
};
Si1Reading si1Readings[SI1_CAPACITY];
enum Si1State { SI1_IDLE, SI1_ARMED, SI1_RECORDING, SI1_COMPLETE, SI1_CANCELLED };
Si1State si1State = SI1_IDLE;
uint32_t si1Id = 0, si1Interval = 500, si1Last = 0, si1ArmedCycle = 0;
uint8_t si1Count = 0, si1Target = 0, si1Offset = 0;
bool si1NextPulse = false, si1IncludeSamples = false;

void si1Cancel() {
  if (si1State == SI1_ARMED || si1State == SI1_RECORDING) si1State = SI1_CANCELLED;
}

const char *si1StateName() {
  switch (si1State) {
    case SI1_ARMED: return "armed";
    case SI1_RECORDING: return "recording";
    case SI1_COMPLETE: return "complete";
    case SI1_CANCELLED: return "cancelled";
    default: return "idle";
  }
}

void si1Service() {
  if (si1State == SI1_ARMED) {
    if (!highPhase || cycleStart == si1ArmedCycle) return;
    si1State = SI1_RECORDING;
    si1Last = micros() - si1Interval;
  }
  if (si1State != SI1_RECORDING || (uint32_t)(micros() - si1Last) < si1Interval) return;
  Si1Reading &r = si1Readings[si1Count];
  r.pin = (si1Count & 1) ? 2 : 1;
  {
    InterruptGuard guard;
    r.started = micros(); r.modeBefore = mode; r.highBefore = highPhase;
    r.cycleBefore = cycleStart; r.outputBefore = channel;
  }
  si1Last = r.started;
  // Leave interrupts enabled while the ADC settles and converts.
  r.raw = analogRead(r.pin == 1 ? A1 : A2);
  {
    InterruptGuard guard;
    r.finished = micros(); r.modeAfter = mode; r.highAfter = highPhase;
    r.cycleAfter = cycleStart; r.outputAfter = channel;
  }
  ++si1Count;
  // A watchdog Stop that happened during conversion retains cancellation.
  if (si1Count == si1Target && si1State == SI1_RECORDING) si1State = SI1_COMPLETE;
}

void si1AppendReply() {
  if (outgoingLength < 2) return;
  size_t used = outgoingLength - 2; // Replace the closing brace and newline.
  int n = snprintf(outgoing + used, sizeof(outgoing) - used,
    ",\"si1_protocol\":1,\"si1\":{\"id\":%lu,\"state\":\"%s\",\"count\":%u,"
    "\"target\":%u,\"interval_us\":%lu,\"next_pulse\":%s,\"adc_bits\":14,"
    "\"offset\":%u,\"samples\":[",
    (unsigned long)si1Id, si1StateName(), si1Count, si1Target,
    (unsigned long)si1Interval, si1NextPulse ? "true" : "false", si1Offset);
  if (n < 0 || (size_t)n >= sizeof(outgoing) - used) { outgoingLength = 0; return; }
  used += n;
  if (si1IncludeSamples) {
    for (uint8_t index = si1Offset; index < si1Count && index < si1Offset + SI1_PAGE; ++index) {
      const Si1Reading &r = si1Readings[index];
      n = snprintf(outgoing + used, sizeof(outgoing) - used,
        "%s[%lu,%lu,%u,%u,%u,%u,%s,%s,%lu,%lu,%u,%u]",
        index == si1Offset ? "" : ",", (unsigned long)r.started, (unsigned long)r.finished,
        r.pin, r.raw, r.modeBefore, r.modeAfter, r.highBefore ? "true" : "false",
        r.highAfter ? "true" : "false", (unsigned long)r.cycleBefore,
        (unsigned long)r.cycleAfter, r.outputBefore, r.outputAfter);
      if (n < 0 || (size_t)n >= sizeof(outgoing) - used) { outgoingLength = 0; return; }
      used += n;
    }
  }
  n = snprintf(outgoing + used, sizeof(outgoing) - used, "]}}\n");
  if (n < 0 || (size_t)n >= sizeof(outgoing) - used) { outgoingLength = 0; return; }
  outgoingLength = used + n;
  si1IncludeSamples = false;
}

void si1Handle(uint32_t request, const char *command) {
  unsigned long id, interval;
  unsigned target, trigger, offset;
  int used = 0;
  if (sscanf(command, "start %lu %u %lu %u %n", &id, &target, &interval, &trigger, &used) == 4 &&
      command[used] == '\0' && id > 0 && id <= UINT32_MAX && target > 0 &&
      target <= SI1_CAPACITY && interval >= 100 && interval <= 1000000 && trigger <= 1) {
    if (si1State == SI1_ARMED || si1State == SI1_RECORDING) {
      reply(request, "error", "An investigation capture is already active"); return;
    }
    if (trigger && mode != PACING && mode != SQUARE) {
      reply(request, "error", "Next-pulse capture requires an existing pulse train"); return;
    }
    si1Id = (uint32_t)id; si1Target = target; si1Interval = interval;
    si1Count = si1Offset = 0; si1NextPulse = trigger;
    si1ArmedCycle = cycleStart;
    si1Last = micros() - si1Interval;
    si1State = trigger ? SI1_ARMED : SI1_RECORDING;
  } else if ((used = 0, sscanf(command, "fetch %lu %u %n", &id, &offset, &used)) == 2 &&
             command[used] == '\0' && id == si1Id && offset <= si1Count) {
    si1Offset = offset; si1IncludeSamples = true;
  } else if ((used = 0, sscanf(command, "cancel %lu %n", &id, &used)) == 1 &&
             command[used] == '\0' && id == si1Id) {
    si1Cancel();
  } else {
    reply(request, "error", "Invalid investigation capture request"); return;
  }
  lastContact = millis(); hostSeen = true;
  reply(request, "ok", "");
}
