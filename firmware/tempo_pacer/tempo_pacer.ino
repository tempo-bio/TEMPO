// TEMPO AU26 V2 -- Uno R4 Minima, OPA340 + 4053, optional 4051.
// Host protocol: <unsigned request id> <command>\n ; newline JSON replies.
// No dynamic allocation or blocking Serial.print calls in the pulse loop.
#include <Arduino.h>
#include <FspTimer.h>
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

constexpr float DAC_REFERENCE_V = 4.77f; // Measure this board's rail; see setup guide.
constexpr uint32_t WATCHDOG_MS = 2000;
constexpr uint8_t SELECT_PIN = 2, DISABLE_PIN = 3, MUX_DISABLE = 4;
constexpr uint8_t ADDRESS[3] = {5, 6, 7};
enum Mode { REST, PACING, CONSTANT, SQUARE, ISOLATED };
volatile Mode mode = REST;
float voltage = .6f, frequency = 1.f, widthMs = 2.f;
volatile uint8_t channel = 0;
volatile bool highPhase = false, cycling = false;
volatile uint32_t periodUs = 1000000, highUs = 2000, cycleStart = 0, phaseStart = 0;
volatile uint32_t lastContact = 0;
volatile bool hostSeen = false;
FspTimer edgeTimer;
bool timerReady = false;

// Restore the previous interrupt mask, including when called from the timer ISR.
struct InterruptGuard {
  uint32_t previous;
  InterruptGuard() : previous(__get_PRIMASK()) { __disable_irq(); }
  ~InterruptGuard() { if (!previous) __enable_irq(); }
};
char input[128];
size_t inputLength = 0;
bool inputOverflow = false;
char outgoing[640];
size_t outgoingLength = 0, outgoingOffset = 0;

void stimulus(bool high) {
  digitalWrite(SELECT_PIN, high ? HIGH : LOW);
  highPhase = high;
}

void stopOutput() {
  InterruptGuard guard;
  mode = REST;
  stimulus(false);
  digitalWrite(DISABLE_PIN, LOW);
  cycling = false;
}

void selectChannel(uint8_t target) {
  // Break before make; channel changes are accepted only at rest.
  stimulus(false);
  digitalWrite(MUX_DISABLE, HIGH);
  channel = target;
  for (uint8_t bit = 0; bit < 3; ++bit)
    digitalWrite(ADDRESS[bit], (target & (1 << bit)) ? HIGH : LOW);
  digitalWrite(MUX_DISABLE, LOW);
}

void writeVoltage() {
  analogWrite(A0, constrain((int)lroundf(voltage * 4095.f / DAC_REFERENCE_V), 0, 4095));
}

void beginHigh() {
  stimulus(true);
  cycleStart = phaseStart = micros();
}

void startTrain(Mode nextMode, bool allChannels = false) {
  InterruptGuard guard;
  stopOutput();
  mode = nextMode;
  cycling = allChannels;
  periodUs = nextMode == SQUARE ? 100000 : (uint32_t)lroundf(1000000.f / frequency);
  highUs = nextMode == SQUARE ? 50000 : (uint32_t)lroundf(widthMs * 1000.f);
  beginHigh();
}

void serviceOutput() {
  if (hostSeen && (uint32_t)(millis() - lastContact) >= WATCHDOG_MS) {
    stopOutput();
    hostSeen = false; // A subsequent heartbeat never restarts output.
  }
  if (mode != PACING && mode != SQUARE) return;
  uint32_t now = micros();
  if (highPhase && (uint32_t)(now - phaseStart) >= highUs) {
    stimulus(false);
  } else if (!highPhase && (uint32_t)(now - cycleStart) >= periodUs) {
    if (cycling) selectChannel((channel + 1) & 7);
    beginHigh(); // No catch-up pulses after a late iteration.
  }
}

void timerTick(timer_callback_args_t *) {
  serviceOutput();
}

const char *modeName() {
  switch (mode) {
    case PACING: return "PACING";
    case CONSTANT: return "CONSTANT_STIM";
    case SQUARE: return "SQUARE_10HZ";
    case ISOLATED: return "DISCONNECTED_HIZ";
    default: return "REST_DISCHARGE";
  }
}

void reply(uint32_t id, const char *result = "ok", const char *error = "") {
  // A stop must still be acknowledged if an old response stalled. Terminate
  // its partial JSON line before the new response. Commands never wait on TX.
  bool partial = outgoingOffset > 0 && outgoingOffset < outgoingLength;
  int n = snprintf(outgoing, sizeof(outgoing),
    "%s{\"id\":%lu,\"device\":\"TEMPO_AU26_V2\",\"protocol\":1,"
    "\"firmware\":\"1.0.0\",\"result\":\"%s\",\"error\":\"%s\","
    "\"mode\":\"%s\",\"amplitude_v\":%.6f,\"frequency_hz\":%.6f,"
    "\"pulse_width_ms\":%.3f,\"channel\":%u,\"cycling\":%s,"
    "\"uptime_ms\":%lu,\"watchdog_ms\":2000,\"dac_reference_v\":%.4f}\n",
    partial ? "\n" : "", (unsigned long)id, result, error, modeName(),
    voltage, frequency, widthMs, channel, cycling ? "true" : "false",
    (unsigned long)millis(), DAC_REFERENCE_V);
  outgoingOffset = 0;
  outgoingLength = n > 0 && (size_t)n < sizeof(outgoing) ? (size_t)n : 0;
}

void handle(char *line) {
  char *end = nullptr;
  unsigned long id = strtoul(line, &end, 10);
  if (end == line || *end != ' ') return;
  const char *command = end + 1;
  while (*command == ' ') ++command;
  bool known = true;
  if (!strcmp(command, "hello")) {
    stopOutput(); // Connecting/reconnecting can never energize an output.
  } else if (!strcmp(command, "stop")) {
    stopOutput();
  } else if (!strcmp(command, "isolate")) {
    stopOutput();
    digitalWrite(DISABLE_PIN, HIGH);
    mode = ISOLATED;
  } else if (!strcmp(command, "ping") || !strcmp(command, "status")) {
    // Status does not alter mode.
  } else if (!strncmp(command, "set ", 4)) {
    if (mode != REST && mode != ISOLATED) {
      reply(id, "error", "Stop output before changing settings"); return;
    }
    float v, hz, ms;
    int ch, confirmed, used = 0;
    int parsed = sscanf(command + 4, "%f %f %f %d %d %n", &v, &hz, &ms, &ch, &confirmed, &used);
    if (parsed != 5 || command[4 + used] != '\0' || !isfinite(v) || !isfinite(hz) || !isfinite(ms) ||
        v < 0 || v > 1 || hz < .1f || hz > 100 || ms < .1f || ms * hz >= 1000 ||
        ch < 0 || ch > 7 || (confirmed != 0 && confirmed != 1) ||
        (uint32_t)lroundf(ms * 1000.f) >= (uint32_t)lroundf(1000000.f / hz)) {
      reply(id, "error", "Invalid voltage frequency width or channel"); return;
    }
    if (!confirmed && (v > .8f || hz > 3 || ms > 10 || ms * hz > 150)) {
      reply(id, "confirmation_required", "V2 device confirmation thresholds exceeded"); return;
    }
    // Apply the complete tuple only after all checks; no partial setting changes.
    stopOutput();
    voltage = v; frequency = hz; widthMs = ms;
    selectChannel((uint8_t)ch);
    writeVoltage();
  } else if (!strcmp(command, "pace") || !strcmp(command, "cycle") ||
             !strcmp(command, "constant") || !strcmp(command, "square")) {
    if (!timerReady) {
      reply(id, "error", "Pulse timer unavailable - output remains stopped"); return;
    }
    if (!hostSeen || mode != REST) {
      reply(id, "error", "Handshake and stop output before starting"); return;
    }
    if (!strcmp(command, "constant")) {
      InterruptGuard guard;
      mode = CONSTANT; stimulus(true);
    } else {
      startTrain(!strcmp(command, "square") ? SQUARE : PACING, !strcmp(command, "cycle"));
    }
  } else {
    known = false;
    reply(id, "error", "Unknown command");
  }
  if (known) {
    lastContact = millis();
    hostSeen = true;
    reply(id);
  }
}

void setup() {
  // Write latches before enabling outputs to avoid a disable-pin low transient.
  digitalWrite(DISABLE_PIN, HIGH); pinMode(DISABLE_PIN, OUTPUT);
  digitalWrite(SELECT_PIN, LOW); pinMode(SELECT_PIN, OUTPUT);
  digitalWrite(MUX_DISABLE, HIGH); pinMode(MUX_DISABLE, OUTPUT);
  for (uint8_t pin : ADDRESS) { digitalWrite(pin, LOW); pinMode(pin, OUTPUT); }
  analogWriteResolution(12);
  writeVoltage();
  selectChannel(0);
  stopOutput();
  // An independent 20 us tick owns pulse edges and the communication watchdog.
  // Serial parsing/JSON formatting cannot hold a pulse high. No timer is stolen
  // from PWM or the core clock; output stays off if allocation fails.
  uint8_t timerType;
  int8_t timerIndex = FspTimer::get_available_timer(timerType);
  if (timerIndex >= 0) {
    timerReady = edgeTimer.begin(TIMER_MODE_PERIODIC, timerType, (uint8_t)timerIndex,
                                50000.f, 0.f, timerTick) &&
                 edgeTimer.setup_overflow_irq(8) && edgeTimer.open() && edgeTimer.start();
  }
  Serial.begin(115200);
}

void loop() {
  // Receive commands in every pulse phase, including long high phases. Bound
  // work per pass; no readline, delay, flush, or diagnostic printing here.
  for (uint8_t n = 0; n < 8 && Serial.available(); ++n) {
    char ch = (char)Serial.read();
    if (ch == '\r') continue;
    if (ch == '\n') {
      if (!inputOverflow && inputLength) { input[inputLength] = '\0'; handle(input); }
      inputLength = 0; inputOverflow = false;
      break;
    }
    if (inputLength < sizeof(input) - 1) input[inputLength++] = ch;
    else inputOverflow = true;
  }
  // Only write the bytes the USB implementation says it can accept now.
  if (outgoingOffset < outgoingLength) {
    int capacity = Serial.availableForWrite();
    size_t amount = outgoingLength - outgoingOffset;
    if (amount > 8) amount = 8;
    if (capacity > 0) {
      if (amount > (size_t)capacity) amount = (size_t)capacity;
      outgoingOffset += Serial.write((const uint8_t *)outgoing + outgoingOffset, amount);
    }
  }
}
