#include <cassert>
#include <iostream>
#define TEMPO_INVESTIGATION_1 1
#include "../../firmware/tempo_pacer/tempo_pacer.ino"

void runFor(uint64_t us) {
  uint64_t end=fixtureUs+us;
  while(fixtureUs<end){ fixtureUs+=20; timerTick(nullptr); loop(); }
}
std::string send(const std::string &command) {
  Serial.output.clear();
  for(char c: command+"\n") Serial.input.push_back(c);
  for(int i=0;i<2000;++i) loop();
  return Serial.output;
}
int main() {
  fixtureAdcTick=[](){timerTick(nullptr);};
  setup(); runFor(10000); assert(fixtureAdcReads==0);
  assert(send("1 hello").find("\"si1_protocol\":1")!=std::string::npos);
  assert(send("2 si1 start 1 8 500 1").find("\"result\":\"error\"")!=std::string::npos);
  assert(send("3 si1 start 1 8 500 0").find("\"result\":\"ok\"")!=std::string::npos);
  runFor(10000); assert(si1State==SI1_COMPLETE && si1Count==8 && mode==REST);
  std::cout<<send("4 si1 fetch 1 0");
  std::cout<<send("5 si1 fetch 1 6");
  assert(si1Readings[0].raw==1024 && si1Readings[1].raw==64);
  assert(si1Readings[0].pin==1 && si1Readings[1].pin==2);
  send("6 set 0.6 100 0.1 0 1"); send("7 pace");
  send("8 si1 start 2 64 100 1"); assert(si1State==SI1_ARMED);
  runFor(11000); assert(si1Count>0);
  send("9 stop"); assert(mode==REST && !highPhase && si1State==SI1_CANCELLED);
  auto count=si1Count; runFor(10000); assert(si1Count==count);
  send("10 si1 start 3 64 1000000 0"); runFor(100);
  send("11 si1 cancel 3"); assert(si1State==SI1_CANCELLED && mode==REST);
  send("12 pace"); send("13 si1 start 4 64 1000000 0");
  runFor(2020000); assert(mode==REST && si1State==SI1_CANCELLED && !hostSeen);
  send("14 hello"); send("15 si1 start 5 8 500 0");
  runFor(10000); Serial.capacity=0;
  send("16 si1 fetch 5 0"); runFor(2020000); assert(mode==REST);
  Serial.capacity=64; send("17 hello");
  assert(send("18 si1 fetch 999 0").find("\"result\":\"error\"")!=std::string::npos);
  assert(send("19 si1 start 6 65 500 0").find("\"result\":\"error\"")!=std::string::npos);
  assert(send("20 si1 start 6 8 99 0").find("\"result\":\"error\"")!=std::string::npos);
  std::cout<<"Investigation capture checks passed\n";
}
