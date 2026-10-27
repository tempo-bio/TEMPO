#pragma once
#include <cstdint>
struct timer_callback_args_t {};
constexpr int TIMER_MODE_PERIODIC=1;
class FspTimer {
public:
  static int8_t get_available_timer(uint8_t &type){ type=0; return 0; }
  bool begin(int,uint8_t,uint8_t,float,float,void(*)(timer_callback_args_t*)){ return true; }
  bool setup_overflow_irq(int){ return true; }
  bool open(){ return true; }
  bool start(){ return true; }
};
