// Minimal host fixture for exercising the actual sketch; no Arduino emulation claims.
#pragma once
#include <algorithm>
#include <cstdint>
#include <deque>
#include <string>
constexpr int HIGH=1,LOW=0,OUTPUT=1,A0=14;
inline uint64_t fixtureUs=0;
inline int fixturePins[32]={};
inline uint32_t fixtureMask=0;
inline uint32_t micros(){ return (uint32_t)fixtureUs; }
inline uint32_t millis(){ return (uint32_t)(fixtureUs/1000); }
inline uint32_t __get_PRIMASK(){ return fixtureMask; }
inline void __disable_irq(){ fixtureMask=1; }
inline void __enable_irq(){ fixtureMask=0; }
inline void pinMode(int,int){}
inline void digitalWrite(int pin,int value){ fixturePins[pin]=value; }
inline void analogWrite(int pin,int value){ fixturePins[pin]=value; }
inline void analogWriteResolution(int){}
template<typename T> T constrain(T value,T low,T high){ return std::max(low,std::min(value,high)); }
struct FixtureSerial {
  std::deque<char> input;
  std::string output;
  int capacity=64;
  void begin(int){}
  int available(){ return (int)input.size(); }
  char read(){ char c=input.front(); input.pop_front(); return c; }
  int availableForWrite(){ return capacity; }
  size_t write(const uint8_t *bytes,size_t count){ output.append((const char*)bytes,count); return count; }
};
inline FixtureSerial Serial;
