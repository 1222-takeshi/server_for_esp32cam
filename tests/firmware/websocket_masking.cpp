// The appended functions come verbatim from the hash-verified build dependency.
#include <algorithm>
#include <cassert>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <limits>
#include <vector>
#define WEBSOCKETS_USE_BIG_MEM
#define WEBSOCKETS_MAX_HEADER_SIZE 14
#define DEBUG_WEBSOCKETS(...) ((void)0)
#define NODEBUG_WEBSOCKETS
#define WEBSOCKETS_TCP_TIMEOUT 10
#define WEBSOCKETS_YIELD() (++ticks)
#define GET_FREE_HEAP heap
#define bit(x) (1u << (x))
static unsigned long ticks;
static size_t heap = 10000, rngCalls, allocations, frees;
static bool failAllocation;
static void *testMalloc(size_t n) { if(failAllocation) return nullptr; ++allocations; return std::malloc(n); }
static void testFree(void *p) { ++frees; std::free(p); }
static unsigned long millis() { return ++ticks; }
static void esp_fill_random(void *out, size_t n) {
  assert(n == 4); ++rngCalls;
  for(size_t i=0; i<n; ++i) static_cast<uint8_t *>(out)[i] = static_cast<uint8_t>(rngCalls * 31 + i);
}
struct TCP {
  std::vector<uint8_t> wire;
  size_t chunk = SIZE_MAX, failAfter = SIZE_MAX, calls = 0;
  bool active = true;
  bool connected() { return active; }
  size_t write(const uint8_t *data, size_t n) {
    ++calls;
    if(wire.size() >= failAfter) return 0;
    n = std::min(n, std::min(chunk, failAfter - wire.size()));
    wire.insert(wire.end(), data, data+n); return n;
  }
};
enum WSopcode_t { WSop_continuation=0, WSop_text=1, WSop_binary=2, WSop_close=8, WSop_ping=9, WSop_pong=10 };
constexpr int WSC_CONNECTED = 1;
struct WSclient_t { TCP *tcp; int status=1; bool cIsClient=true; };
struct WebSockets {
  size_t disconnected=0;
  uint8_t createHeader(uint8_t *, WSopcode_t, size_t, bool, uint8_t[4], bool);
  bool sendFrameHeader(WSclient_t *, WSopcode_t, size_t, bool);
  bool sendFrame(WSclient_t *, WSopcode_t, uint8_t *, size_t, bool=true, bool=false);
  size_t write(WSclient_t *, uint8_t *, size_t);
  void clientDisconnect(WSclient_t *c) { ++disconnected; c->tcp->active=false; c->tcp=nullptr; c->status=0; }
};
#define malloc testMalloc
#define free testFree
// ACTUAL_FUNCTIONS
#undef malloc
#undef free

static std::vector<uint8_t> decode(const TCP &tcp, size_t length, bool masked, WSopcode_t op) {
  const auto &v=tcp.wire;
  assert(v.size() >= 2 && v[0] == (0x80 | op));
  assert(bool(v[1] & 0x80) == masked);
  size_t p=2; uint64_t size=v[1]&127;
  if(size == 126 || size == 127) {
    const size_t count = size == 126 ? 2 : 8;
    assert(v.size() >= p+count); size=0;
    for(size_t i=0; i<count; ++i) size=(size << 8) | v[p++];
    assert(count == (length <= 65535 ? 2u : 8u));
  }
  assert(size == length && (v[1]&127) == (length < 126 ? length : length <= 65535 ? 126 : 127));
  const size_t key=p; if(masked) p+=4;
  assert(v.size() == p+length);
  std::vector<uint8_t> out(length);
  for(size_t i=0; i<length; ++i) out[i]=v[p+i] ^ (masked ? v[key+i%4] : 0);
  return out;
}
int main() {
  WebSockets ws;
  for(size_t length : {size_t(0), size_t(1), size_t(125), size_t(126), size_t(1399), size_t(1400),
                       size_t(65534), size_t(65535), size_t(65536), size_t(131120)}) {
    for(bool headroom : {false,true}) for(bool client : {false,true}) for(size_t freeHeap : {size_t(0),size_t(10000)}) {
      heap=freeHeap;
      std::vector<uint8_t> expected(length);
      for(size_t i=0; i<length; ++i) expected[i]=static_cast<uint8_t>(i*37);
      std::vector<uint8_t> storage(16+14+length+16,0xa5);
      auto *payload=storage.data()+16;
      std::copy(expected.begin(),expected.end(),payload+(headroom?14:0));
      const auto before=storage;
      TCP tcp; tcp.chunk=137; WSclient_t c{&tcp,1,client};
      const size_t rngBefore=rngCalls;
      assert(ws.sendFrame(&c,WSop_binary,payload,length,true,headroom));
      assert(rngCalls == rngBefore+(client?1:0));
      assert(decode(tcp,length,client,WSop_binary) == expected);
      if(!headroom) assert(storage == before); // const caller bytes remain untouched.
      assert(std::all_of(storage.begin(),storage.begin()+16,[](uint8_t v){return v==0xa5;}));
      assert(std::all_of(storage.end()-16,storage.end(),[](uint8_t v){return v==0xa5;}));
      assert(allocations==frees);
      if(client) { // A second frame must ask for a fresh key, even when empty.
        const auto first=tcp.wire; tcp.wire.clear();
        std::copy(expected.begin(),expected.end(),payload+(headroom?14:0));
        assert(ws.sendFrame(&c,WSop_binary,payload,length,true,headroom));
        assert(tcp.wire != first && decode(tcp,length,true,WSop_binary)==expected);
      }
    }
  }
  heap=0;
  for(auto op : {WSop_text,WSop_ping,WSop_pong,WSop_close}) for(size_t n : {size_t(0),size_t(2),size_t(125)}) {
    TCP tcp; WSclient_t c{&tcp}; std::vector<uint8_t> data(n,0x6c);
    assert(ws.sendFrame(&c,op,n?data.data():nullptr,n));
    assert(decode(tcp,n,true,op)==data);
  }
  TCP tcp; WSclient_t c{&tcp}; uint8_t data[2000]={};
  failAllocation=true;
  assert(!ws.sendFrame(&c,WSop_binary,data,sizeof(data)) && tcp.wire.empty());
  failAllocation=false;
  assert(!ws.sendFrame(nullptr,WSop_binary,data,1));
  assert(!ws.sendFrame(&c,WSop_binary,nullptr,1));
  assert(!ws.sendFrame(&c,WSop_binary,nullptr,0,true,true));
  assert(!ws.sendFrame(&c,WSop_binary,data,UINT32_MAX));
  assert(!ws.sendFrameHeader(&c,WSop_binary,1,true) && tcp.wire.empty());
  WSclient_t missing{nullptr}; assert(!ws.sendFrame(&missing,WSop_binary,data,1));
  for(size_t failAt : {size_t(0),size_t(3),size_t(10),size_t(100)}) {
    TCP broken; broken.failAfter=failAt; WSclient_t b{&broken};
    const auto prior=ws.disconnected;
    assert(!ws.sendFrame(&b,WSop_binary,data,sizeof(data)));
    assert(ws.disconnected==prior+1 && b.tcp==nullptr && allocations==frees);
    const auto sent=broken.wire;
    assert(!ws.sendFrame(&b,WSop_binary,data,1) && broken.wire==sent);
  }
  assert(allocations==frees);
}
