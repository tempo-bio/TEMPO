#include <cassert>
#include <iostream>
#include "../../firmware/tempo_pacer/tempo_pacer.ino"

void advance(uint64_t microseconds) {
  for(uint64_t delta=0;delta<microseconds;delta+=20) {
    fixtureUs+=20;
    timerTick(nullptr);
  }
}

std::string send(const std::string &command) {
  Serial.output.clear();
  for(char c: command+"\n") Serial.input.push_back(c);
  for(int i=0;i<2000;++i) loop();
  return Serial.output;
}

int main() {
  setup();
  assert(mode==REST && fixturePins[SELECT_PIN]==LOW && fixturePins[DISABLE_PIN]==LOW);
  assert(send("1 hello").find("TEMPO_AU26_V2")!=std::string::npos);
  assert(send("2 set 0.9 2 5 3 0").find("confirmation_required")!=std::string::npos);
  assert(voltage==.6f && channel==0); // No partially applied tuple.
  assert(send("3 set 0.9 2 5 3 1").find("\"result\":\"ok\"")!=std::string::npos);
  assert(voltage==.9f && channel==3);
  send("4 pace"); assert(mode==PACING && highPhase);
  advance(5000); assert(!highPhase);
  advance(495000); assert(highPhase);
  send("5 stop"); assert(mode==REST && !highPhase);
  send("6 set 0.6 0.5 1500 0 1"); send("7 pace");
  advance(1000000); assert(highPhase);
  send("8 stop"); assert(mode==REST && !highPhase); // Stop parses during a long high phase.
  send("9 set 0.6 1 2 0 0"); send("10 pace");
  for(int i=0;i<12;++i){ advance(200000); send(std::to_string(11+i)+" ping"); }
  assert(mode==PACING); // Heartbeats preserve a continuous train.
  advance(2020000); assert(mode==REST && !highPhase && !hostSeen);
  send("30 ping"); assert(mode==REST); // Never restart after timeout/reconnect.
  send("31 constant"); assert(mode==CONSTANT && highPhase);
  advance(2020000); assert(mode==REST && !highPhase);
  send("32 hello"); send("33 set 0.6 8 2 2 1"); send("34 cycle");
  assert(channel==2); advance(125000); assert(channel==3);
  send("35 stop");
  assert(send("36 set nan 1 2 0 0").find("\"result\":\"error\"")!=std::string::npos);
  assert(send("37 set 0.5 1 1000 0 0").find("\"result\":\"error\"")!=std::string::npos);
  assert(send("38 set 0.5 1 2 0 0 garbage").find("\"result\":\"error\"")!=std::string::npos);
  send("39 set 0.6 1 2 0 0");
  Serial.capacity=0; send("40 pace");
  assert(highPhase); advance(2000); assert(!highPhase); // Congested TX cannot extend a pulse.
  advance(2020000); assert(mode==REST && !highPhase);
  Serial.capacity=64;
  fixtureUs=4294967000ULL; send("41 hello"); send("42 pace");
  advance(2000); assert(!highPhase); // micros() wrap is handled with unsigned differences.
  std::cout<<"Firmware state, watchdog, cancellation, congestion, and rollover checks passed\n";
}
