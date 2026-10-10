/* Single-owner WinUSB sweep engine. No Python callbacks, allocation or disk I/O
 * in the control loop. All times are QPC seconds; USB completion is not CAN ACK. */
#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <winusb.h>
#include <stdint.h>
#include <string.h>
#include <math.h>

#define API __declspec(dllexport)
typedef struct {
    double rate, f0, f1, duration, amplitude, ramp, home_position, home_duration;
    double settle, home_timeout, position_tolerance, velocity_tolerance;
    double motor_temperature, driver_temperature, feedback_timeout, lease;
    uint32_t id, kp, kd, derivative;
} Config;
typedef struct { double target, kp, kd, position, velocity, torque, mt, bt, at, frequency; } Row;
typedef struct {
    uint64_t tx, rx, rows, skipped, gaps, limited, counters;
    double first_tx, last_tx, max_gap, max_write, first_rx, last_rx, max_submit;
    uint32_t code, win_error, disable_ack;
} Stats;
typedef struct { volatile LONG stop; volatile LONG64 heartbeat, frequency_bits; } Control;

static double seconds(void) {
    LARGE_INTEGER n, f; QueryPerformanceCounter(&n); QueryPerformanceFrequency(&f);
    return (double)n.QuadPart / f.QuadPart;
}
API void sweep_heartbeat(Control *c) {
    LARGE_INTEGER n; QueryPerformanceCounter(&n); InterlockedExchange64(&c->heartbeat, n.QuadPart);
}
API void sweep_stop(Control *c) { InterlockedExchange(&c->stop, 1); }
static void publish_frequency(Control *c, double frequency) {
    LONG64 bits; memcpy(&bits,&frequency,sizeof(bits));
    InterlockedExchange64(&c->frequency_bits,bits);
}
API double sweep_current_frequency(Control *c) {
    LONG64 bits=InterlockedCompareExchange64(&c->frequency_bits,0,0);
    double frequency; memcpy(&frequency,&bits,sizeof(frequency)); return frequency;
}
API double sweep_frequency(const Config *c, double t) {
    if (t>=c->duration+2*c->ramp) return 0;
    if (t<c->ramp) return c->f0;
    if (t<c->ramp+c->duration) return c->f0+(c->f1-c->f0)*(t-c->ramp)/c->duration;
    return c->f1;
}
API uint32_t sweep_size(uint32_t i) {
    return i == 0 ? sizeof(Config) : i == 1 ? sizeof(Row) : i == 2 ? sizeof(Stats) : sizeof(Control);
}
static double envelope(double u) {
    u = fmax(0, fmin(1, u)); return u*u*u*(10+u*(-15+6*u));
}
static double derivative(double u) {
    u = fmax(0, fmin(1, u)); return 30*u*u*(1-u)*(1-u);
}
API void sweep_sample(const Config *c, double t, double *p, double *v) {
    const double tau = 6.2831853071795864769;
    double g, dg, frequency, cycles, x, slope = (c->f1-c->f0)/c->duration;
    t = fmax(0, t);
    if (t >= c->duration+2*c->ramp) { *p = *v = 0; return; }
    if (t < c->ramp) {
        g=envelope(t/c->ramp); dg=derivative(t/c->ramp)/c->ramp;
        frequency=c->f0; cycles=c->f0*t;
    } else if (t < c->ramp+c->duration) {
        x=t-c->ramp; g=1; dg=0; frequency=c->f0+slope*x;
        cycles=c->f0*c->ramp+c->f0*x+.5*slope*x*x;
    } else {
        x=t-c->ramp-c->duration; g=1-envelope(x/c->ramp);
        dg=-derivative(x/c->ramp)/c->ramp; frequency=c->f1;
        cycles=c->f0*c->ramp+.5*(c->f0+c->f1)*c->duration+c->f1*x;
    }
    double angle=tau*remainder(cycles,1);
    *p=c->amplitude*g*sin(angle);
    *v=c->derivative ? c->amplitude*(dg*sin(angle)+g*tau*frequency*cos(angle)) : 0;
}
static uint32_t u32(const unsigned char *p) { uint32_t n; memcpy(&n,p,4); return n; }
static uint16_t u16(const unsigned char *p) { uint16_t n; memcpy(&n,p,2); return n; }
static float f32(const unsigned char *p) { float n; memcpy(&n,p,4); return n; }
static int16_t i16(const unsigned char *p) { int16_t n; memcpy(&n,p,2); return n; }
static int write_packet(WINUSB_INTERFACE_HANDLE usb, UCHAR pipe,
                        const unsigned char *p, ULONG size, Stats *s) {
    ULONG n=0; double t=seconds();
    BOOL ok=WinUsb_WritePipe(usb,pipe,(PUCHAR)p,size,&n,NULL);
    s->max_write=fmax(s->max_write,seconds()-t);
    if (!ok || n!=size) { s->win_error=ok ? ERROR_WRITE_FAULT : GetLastError(); return 0; }
    return 1;
}
static int finish_tx(void *usb, OVERLAPPED *ov, Stats *s, double started,
                     double next_target, double *target, double *due, double period) {
    ULONG n=0;
    if (!WinUsb_GetOverlappedResult(usb,ov,&n,FALSE)) { s->win_error=GetLastError(); return 0; }
    if (n!=76) { s->win_error=ERROR_WRITE_FAULT; return 0; }
    double sent=seconds(); *target=next_target;
    s->max_write=fmax(s->max_write,sent-started);
    if (s->tx) {
        double gap=sent-s->last_tx; s->max_gap=fmax(s->max_gap,gap);
        if (gap>.005) s->gaps++;
    } else s->first_tx=sent;
    s->last_tx=sent; s->tx++;
    double periods=fmax(1,floor((sent-*due)/period)+1);
    s->skipped+=(uint64_t)periods-1; *due+=periods*period;
    return 1;
}

/* Codes: 0 complete, 1 cancelled, 2 USB, 3 malformed/CAN error, 4 motor fault,
 * 5 temperature, 6 stale feedback, 7 lease, 8 zero timeout, 9 buffer full,
 * 10 disable unconfirmed. Save all captured packets up to a failure. */
API int sweep_run(void *usb, void *file, unsigned input, unsigned output,
                  const Config *c, Row *rows, uint64_t capacity, Stats *s, Control *ctl) {
    unsigned char incoming[2048], buffer[4096], tx[76]={0}, disable[76]={0};
    size_t buffered=0;
    OVERLAPPED ov={0}, txov={0};
    int pending=0, tx_pending=0, stage=0, capture=0, done=0, inflight=0;
    double tx_started=0, next_target=0, next_frequency=0, target_frequency=0;
    uint32_t echo=1; double target=0, start=seconds(), due=start, last_rx=start, stable=-1;
    double period=1/c->rate, end=start+c->home_duration;
    LARGE_INTEGER frequency; QueryPerformanceFrequency(&frequency);
    memset(s,0,sizeof(*s));
    ov.hEvent=CreateEventW(NULL,TRUE,FALSE,NULL);
    txov.hEvent=CreateEventW(NULL,TRUE,FALSE,NULL);
    if (!ov.hEvent || !txov.hEvent) { s->code=2; s->win_error=GetLastError(); goto stop_motor; }
    SetThreadPriority(GetCurrentThread(), THREAD_PRIORITY_ABOVE_NORMAL);
    { ULONG timeout=50;
      if (!WinUsb_SetPipePolicy(usb,(UCHAR)output,PIPE_TRANSFER_TIMEOUT,sizeof(timeout),&timeout)) {
          s->code=2; s->win_error=GetLastError(); goto stop_motor;
      }
    }
    { uint32_t id=0x400+c->id; memcpy(tx+4,&id,4); tx[8]=10; tx[10]=6;
      uint16_t kp=(uint16_t)c->kp, kd=(uint16_t)c->kd;
      memcpy(tx+24,&kp,2); memcpy(tx+26,&kd,2);
    }
    while (!done) {
        double now=seconds();
        if (InterlockedCompareExchange(&ctl->stop,0,0)) { s->code=1; break; }
        double heartbeat=(double)InterlockedCompareExchange64(&ctl->heartbeat,0,0)/frequency.QuadPart;
        if (now-heartbeat>c->lease) { s->code=7; break; }
        if (now-last_rx>c->feedback_timeout) { s->code=6; break; }
        if (now>end+c->home_timeout) { s->code=8; break; }
        if (tx_pending) {
            DWORD ready=WaitForSingleObject(txov.hEvent,0);
            if (ready==WAIT_OBJECT_0) {
                tx_pending=0;
                if (!finish_tx(usb,&txov,s,tx_started,next_target,&target,&due,period)) { s->code=2; break; }
                target_frequency=next_frequency; publish_frequency(ctl,target_frequency);
            } else if (ready!=WAIT_TIMEOUT || now-tx_started>.05) {
                s->code=2; s->win_error=ready==WAIT_TIMEOUT ? ERROR_SEM_TIMEOUT : GetLastError(); break;
            }
        }
        if (!tx_pending && now>=due) {
            if (inflight>=4) { s->limited++; due=now+period; }
            else {
                double p,v,t=now-start;
                next_frequency=stage ? sweep_frequency(c,t) : 0;
                if (stage) sweep_sample(c,t,&p,&v);
                else {
                    p=c->home_position*(1-envelope(t/c->home_duration));
                    v=c->derivative ? -c->home_position*derivative(t/c->home_duration)/c->home_duration : 0;
                }
                float pf=(float)p,vf=(float)v;
                memcpy(tx,&echo,4); echo=echo%255+1;
                memcpy(tx+12,&pf,4); memcpy(tx+16,&vf,4);
                ResetEvent(txov.hEvent); next_target=pf; tx_started=seconds();
                BOOL ok=WinUsb_WritePipe(usb,(UCHAR)output,tx,76,NULL,&txov);
                DWORD error=ok ? 0 : GetLastError();
                s->max_submit=fmax(s->max_submit,seconds()-tx_started);
                if (!ok && error!=ERROR_IO_PENDING) { s->code=2; s->win_error=error; break; }
                tx_pending=1; inflight++;
                if (ok || WaitForSingleObject(txov.hEvent,0)==WAIT_OBJECT_0) {
                    tx_pending=0;
                    if (!finish_tx(usb,&txov,s,tx_started,next_target,&target,&due,period)) { s->code=2; break; }
                    target_frequency=next_frequency; publish_frequency(ctl,target_frequency);
                }
            }
        }
        if (!pending) {
            ResetEvent(ov.hEvent);
            BOOL ok=WinUsb_ReadPipe(usb,(UCHAR)input,incoming,sizeof(incoming),NULL,&ov);
            if (!ok && GetLastError()!=ERROR_IO_PENDING) { s->win_error=GetLastError(); s->code=2; break; }
            pending=1;
        }
        DWORD ready=WaitForSingleObject(ov.hEvent,0);
        if (ready==WAIT_OBJECT_0) {
            ULONG n=0;
            if (!WinUsb_GetOverlappedResult(usb,&ov,&n,FALSE)) { s->code=2; s->win_error=GetLastError(); break; }
            pending=0;
            if (buffered+n>sizeof(buffer)) { s->code=3; break; }
            memcpy(buffer+buffered,incoming,n); buffered+=n;
            while (buffered>=12) {
                unsigned char packet[76]; unsigned char *p=packet;
                size_t len=(buffer[10]&2) ? 76 : 20;
                if (buffered<len) break;
                memcpy(packet,buffer,len); buffered-=len; memmove(buffer,buffer+len,buffered);
                uint32_t id=u32(p+4);
                if (p[9]!=0 || (p[10]&1) || p[8]>((p[10]&2)?15:8)) { s->code=3; done=1; break; }
                if (u32(p)==0xffffffff) {
                    if (id & 0x20000000) {
                        /* Only class-free, below-warning counter notifications. */
                        if (id==0x20000000 && !(p[10]&2) && p[8]==8 &&
                            !memcmp(p+12,"\0\0\0\0\0\0",6) && p[18]<96 && p[19]<96) s->counters++;
                        else { s->code=3; done=1; break; }
                    } else if (id==0x190+c->id) {
                        if (!(p[10]&2) || (p[8]!=10 && p[8]!=11)) { s->code=3; done=1; break; }
                        double at=seconds(), pos=f32(p+12), vel=f32(p+16), torque=f32(p+20);
                        double mt=i16(p+24)/10., bt=i16(p+26)/10.;
                        if (!isfinite(pos)||!isfinite(vel)||!isfinite(torque)) { s->code=3; done=1; break; }
                        s->rx++; last_rx=at; if (inflight) inflight--;
                        if (capture) {
                            if (s->rows>=capacity) { s->code=9; done=1; break; }
                            rows[s->rows++]=(Row){target,c->kp/100.,c->kd/100.,pos,vel,torque,mt,bt,at,target_frequency};
                            if (s->rows==1) s->first_rx=at;
                            s->last_rx=at;
                        }
                        if (p[8]==11 && u16(p+28)) { s->code=4; done=1; break; }
                        if (mt>=c->motor_temperature || bt>=c->driver_temperature) { s->code=5; done=1; break; }
                        if (at>=end) {
                            if (fabs(pos)<=c->position_tolerance && fabs(vel)<=c->velocity_tolerance) {
                                if (stable<0) stable=at;
                                if (at-stable>=c->settle) {
                                    if (stage) done=1;
                                    else { stage=capture=1; start=at; end=at+c->duration+2*c->ramp; stable=-1; }
                                }
                            } else stable=-1;
                        }
                    }
                }
            }
        } else if (ready!=WAIT_TIMEOUT) { s->code=2; s->win_error=GetLastError(); break; }
        else if (due-seconds()>.002) Sleep(1);
        else YieldProcessor();
    }
stop_motor:
    /* Resolve/cancel OUT before disable; never reuse its buffer while pending. */
    if (tx_pending) {
        ULONG n=0;
        CancelIoEx(file,&txov);
        if (WinUsb_GetOverlappedResult(usb,&txov,&n,TRUE) && n==76) {
            target=next_target; target_frequency=next_frequency;
            publish_frequency(ctl,target_frequency);
        }
        tx_pending=0;
    }
    /* The native owner also disables on lease expiry even if Python is stalled. */
    { uint32_t id=0x600+c->id; memcpy(disable,&echo,4); memcpy(disable+4,&id,4);
      disable[8]=8; disable[10]=6; disable[12]=0x2b; disable[13]=2; disable[14]=0x20; disable[16]=2;
    }
    if (write_packet(usb,(UCHAR)output,disable,76,s) && ov.hEvent) {
        double stop_deadline=seconds()+.2;
        while (seconds()<stop_deadline && !s->disable_ack) {
            /* Consume complete packets already buffered before asking USB again. */
            while (buffered>=12) {
                unsigned char *p=buffer; size_t len=(p[10]&2)?76:20;
                if (buffered<len) break;
                if (u32(p)==0xffffffff && p[9]==0 && !(p[10]&1)) {
                    uint32_t id=u32(p+4);
                    if (id==0x580+c->id && p[8]==8 &&
                        !memcmp(p+12,"\x60\x02\x20\x00",4)) s->disable_ack=1;
                    else if (capture && id==0x190+c->id && (p[10]&2) && (p[8]==10 || p[8]==11)) {
                        double pos=f32(p+12),vel=f32(p+16),torque=f32(p+20),at=seconds();
                        if (isfinite(pos)&&isfinite(vel)&&isfinite(torque) && s->rows<capacity) {
                            rows[s->rows++]=(Row){target,c->kp/100.,c->kd/100.,pos,vel,torque,i16(p+24)/10.,i16(p+26)/10.,at,target_frequency};
                            s->rx++; if(s->rows==1) s->first_rx=at; s->last_rx=at;
                        } else if (!s->code) s->code=9;
                    }
                }
                buffered-=len; memmove(buffer,buffer+len,buffered);
            }
            if (s->disable_ack) break;
            if (!pending) {
                ResetEvent(ov.hEvent);
                BOOL ok=WinUsb_ReadPipe(usb,(UCHAR)input,incoming,sizeof(incoming),NULL,&ov);
                if (!ok && GetLastError()!=ERROR_IO_PENDING) break;
                pending=1;
            }
            if (WaitForSingleObject(ov.hEvent,1)==WAIT_OBJECT_0) {
                ULONG n=0;
                if (!WinUsb_GetOverlappedResult(usb,&ov,&n,FALSE)) break;
                pending=0;
                if(buffered+n>sizeof(buffer)) break;
                memcpy(buffer+buffered,incoming,n); buffered+=n;
            }
        }
    }
    if (!s->disable_ack && !s->code) s->code=10;
    if (pending) {
        ULONG n=0; CancelIoEx(file,&ov); WinUsb_GetOverlappedResult(usb,&ov,&n,TRUE);
    }
    if (ov.hEvent) CloseHandle(ov.hEvent);
    if (txov.hEvent) CloseHandle(txov.hEvent);
    return (int)s->code;
}
