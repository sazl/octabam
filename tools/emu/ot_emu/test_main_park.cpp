// Firmware-free execution: every instruction below is synthetic test code.
#include <cstdio>
#include <vector>
#include <fstream>
#include <string>
#include <filesystem>
#include <unistd.h>
#include <sys/wait.h>
#include "machine.h"
#include "rtos.h"
#include "mc68k/Musashi/m68k.h"
#include "mc68k/cpuState.h"

namespace {
int failures = 0;
void check(const char* what, bool ok) {
    std::printf("[%s] %s\n", ok ? "PASS" : "FAIL", what);
    failures += !ok;
}
constexpr uint32_t work = 0x47700000, park = work + 14;
constexpr uint32_t count = 0x46001000, borrowed = work + 0x100;
void seed(ot::Machine& m) {
    m.poke32(ot::g_vbr + 0x80, ot::g_sched);
    m.poke32(ot::g_vbr + 4 * 171, ot::g_sched);
    m.write16(ot::g_mainSpin, 0x60fe); // bra.s .
    m.write16(ot::g_mainSpin - 6, 0x4ef9);
    m.poke32(ot::g_mainSpin - 4, work);
    // move.l count,d0; addq.l #1,d0; move.l d0,count; bra.s .
    m.write16(work, 0x2039); m.poke32(work + 2, count);
    m.write16(work + 6, 0x5280);
    m.write16(work + 8, 0x23c0); m.poke32(work + 10, count);
    m.write16(park, 0x60f0); // bra.s work; this exact branch is the declared park
    m.write16(borrowed, 0x702a); m.write16(borrowed + 2, 0x4e75); // moveq #42,d0; rts
    m.setPC(work);
    m.setA7(0x47000000);
}
void timer(ot::Machine& m) {
    m.write16(ot::g_pit0 + 2, 644);
    m.write16(ot::g_pit0, ot::Pit::EN | ot::Pit::RLD | (11u << 8));
}
}
int cli(const char* executable) {
    char dir[] = "/tmp/ot-main-park-XXXXXX";
    if(!mkdtemp(dir)) return 1;
    const std::string prefix = std::string(dir) + "/";
    std::vector<uint8_t> image(0x20000);
    auto word = [&](uint32_t addr, uint16_t value) { image.at(addr - ot::Machine::g_imageBase) = value >> 8; image.at(addr - ot::Machine::g_imageBase + 1) = value; };
    auto lng = [&](uint32_t addr, uint32_t value) { word(addr, value >> 16); word(addr + 2, value); };
    uint32_t p = ot::Machine::g_imageBase;
    for(auto v : {0x80u, ot::g_vbr + 0x80, ot::g_vbr + 4 * 171}) {
        word(p, 0x23fc); lng(p + 2, ot::g_sched); lng(p + 6, v); p += 10;
    }
    word(p, 0x4ef9); lng(p + 2, ot::g_handoff);
    word(ot::g_handoff, 0x4e40); // Machine::run stops on trap #0
    word(ot::g_sched, 0x4ef9); lng(ot::g_sched + 2, 0x40002000);
    p = 0x40002000;
    for(auto value : {0x00, 0xff, 0x74}) {
        word(p, 0x13fc); word(p + 2, value); lng(p + 4, 0xfc06400c); p += 8;
    }
    for(auto pair : {std::pair<uint32_t, uint16_t>{ot::g_pit0 + 2, 644}, {ot::g_pit0, ot::Pit::EN | ot::Pit::RLD | (11u << 8)}}) {
        word(p, 0x33fc); word(p + 2, pair.second); lng(p + 4, pair.first); p += 8;
    }
    word(p, 0x4ef9); lng(p + 2, ot::g_mainSpin);
    word(ot::g_mainSpin, 0x60fe);
    const std::string imageFile = prefix + "synthetic.bin", panelFile = prefix + "panel.bin";
    { std::ofstream f(imageFile, std::ios::binary); f.write(reinterpret_cast<const char*>(image.data()), image.size()); }
    auto run = [&](std::vector<std::string> args) {
        args.insert(args.begin(), executable);
        std::fflush(nullptr);
        const auto pid = fork();
        if(pid == 0) {
            freopen((prefix + "cli.log").c_str(), "w", stdout);
            freopen((prefix + "cli.log").c_str(), "a", stderr);
            std::vector<char*> av;
            for(auto& a : args) av.push_back(a.data());
            av.push_back(nullptr); execv(executable, av.data()); _exit(127);
        }
        int status = 0;
        if(pid < 0 || waitpid(pid, &status, 0) != pid || !WIFEXITED(status)) return -1;
        return WEXITSTATUS(status);
    };
    for(const auto* value : {"", "0:2", "-2:4", "1:2", "2:2", "2:4:6", "0x100000000:2", "0x40002000:xyz"})
        check("malformed main-park rejected before image access", run({"--image", prefix + "absent", "--main-park", value}) == 2);
    check("unmapped main-park rejected after synthetic boot", run({"--image", imageFile, "--main-park", "0x60000000:0x40002000"}) == 2);
    check("positive CLI configuration and raw panel dump survive USB hold exit", run({"--image", imageFile, "--ms", "0.1", "--main-park", "0x4001fc9c:0x40002000", "--panel-tx", panelFile, "--usb-host", prefix + "usb.sock", "--usb-hold-ms", "0"}) == 0);
    std::ifstream f(panelFile, std::ios::binary);
    const std::vector<uint8_t> tx((std::istreambuf_iterator<char>(f)), {});
    check("panel-tx is exact actual UART bytes including zero and ff", tx == std::vector<uint8_t>({0, 0xff, 0x74}));
    check("panel-tx output failures reported", run({"--image", imageFile, "--ms", "0.1", "--panel-tx", prefix + "absent/panel.bin"}) != 0);
    if(failures) std::printf("CLI fixture retained at %s\n", dir);
    else std::filesystem::remove_all(dir);
    return failures ? 1 : 0;
}
int main(int argc, char** argv) {
    if(argc == 2) return cli(argv[1]);

    {
        ot::Machine m(std::vector<uint8_t>{}); seed(m);
        ot::Rtos r(m); r.install(); timer(m);
        check("arbitrary detour executes actual memory increment", r.runUntil(1, [&] { return m.peek32(count) == 1; }) == ot::Rtos::Stop::Gate);
    }
    {
        ot::Machine m(std::vector<uint8_t>{}); seed(m);
        ot::Rtos r(m);
        check("explicit park configured", r.setMainPark(park, work));
        r.install(); timer(m);
        check("only exact park is idle", r.atSpin(park) && !r.atSpin(work) && !r.atSpin(park + 2) && !r.atSpin(ot::g_mainSpin));
        check("runToMainSpin executes work then stops at park", r.runToMainSpin() == ot::Rtos::Stop::Gate && m.pc() == park && m.peek32(count) == 1);
        check("repeated timer opportunities execute real counter stores", r.runUntil(30, [&] { return m.peek32(count) >= 4; }) == ot::Rtos::Stop::Gate && r.idleSkips() == 3 && r.pit0Fired() == 3);
        check("per-instruction stop catches real store before park", m.pc() == park && m.peek32(count) == 4);
        r.watchMem(count, 4);
        check("event-stop bursts also execute work after sleep", r.runUntil(30, [&] { return m.peek32(count) >= 6; }, ot::Rtos::Changes::OnEvent) == ot::Rtos::Stop::Gate && m.peek32(count) == 6);
        check("park reachable before borrowed call", r.runToMainSpin() == ot::Rtos::Stop::Gate && m.pc() == park);
        uint32_t d0 = 0;
        const auto before = m.peek32(count), sp = m.getA7();
        check("borrowed call executes and returns to resume", r.callAsMain(borrowed, {}, d0) && d0 == 42 && m.pc() == work && m.getA7() == sp);
        check("borrowed call never fabricates publisher work", m.peek32(count) == before);
        check("publisher restarts after borrowed call", r.runToMainSpin() == ot::Rtos::Stop::Gate && m.pc() == park && m.peek32(count) == before + 1);
        check("publisher continues through later sleeps", r.runUntil(30, [&] { return m.peek32(count) >= before + 4; }) == ot::Rtos::Stop::Gate);
        check("late reconfiguration rejected", !r.setMainPark(park + 2, work));
    }
    {
        ot::Machine m(std::vector<uint8_t>{}); seed(m);
        // Synthetic interrupt handler, preserving d0 around a real count.
        uint32_t p = ot::g_sched;
        m.write16(p, 0x2f00); p += 2; // move.l d0,-(sp)
        m.write16(p, 0x2039); m.poke32(p + 2, count + 4); p += 6;
        m.write16(p, 0x5280); p += 2;
        m.write16(p, 0x23c0); m.poke32(p + 2, count + 4); p += 6;
        m.write16(p, 0x33fc); m.write16(p + 2, 0xb0f); m.poke32(p + 4, ot::g_pit0); p += 8;
        m.write16(p, 0x201f); m.write16(p + 2, 0x4e73); // restore d0; rte
        m68k_set_reg(m.getCpuState(), M68K_REG_VBR, ot::g_vbr);
        m68k_set_reg(m.getCpuState(), M68K_REG_SR, 0x2000);
        ot::Rtos r(m);
        check("interrupt fixture configured", r.setMainPark(park, work));
        r.install(); timer(m);
        m.write8(ot::g_intc1 + 0x40 + 43, 3);
        m.write8(ot::g_intc1 + 0x1d, 43);
        m.write16(ot::g_pit0, 0xb0b);
        check("real timer interrupt returns to executable continuation", r.runUntil(30, [&] { return m.peek32(count) == 4; }) == ot::Rtos::Stop::Gate && m.peek32(count + 4) == 3 && r.idleSkips() == 3);
    }
    {
        ot::Machine m(std::vector<uint8_t>{}); seed(m);
        // Authored module instruction bytes from m68k-elf-as -mcpu=5475
        // modules/cfmeter-idle/idle.s, with synthetic data relocations.
        const std::vector<uint8_t> meter = {
            0x4e,0xb9,0x40,0x09,0x8a,0x2c,0x45,0xf9,0x46,0,0x10,0,
            0x24,0x39,0xfc,0x07,0xc0,0x0c,0x2a,0x3c,0x7f,0xff,0xff,0xff,
            0x2c,0x05,0x20,0x39,0xfc,0x07,0xc0,0x0c,0x22,0x00,0x92,0x82,
            0x24,0x00,0xb2,0x85,0x64,0x0e,0x2a,0x01,0x23,0xc5,0x46,0,0x10,4,
            0x2c,0x05,0xdc,0x86,0x50,0x86,0xb2,0x86,0x64,0xde,0xd3,0x92,0x60,0xda
        };
        for(size_t i = 0; i < meter.size(); ++i) m.write8(work + i, meter[i]);
        ot::Rtos r(m); r.install(); timer(m);
        check("recognized CF METER IDLE retains declared legacy range", r.runToMainSpin() == ot::Rtos::Stop::Gate && m.pc() == work && r.atSpin(work + 0x7e) && !r.atSpin(work + 0x80));
        check("legacy idle skip retains clock and PC behavior", r.run(12, false) == ot::Rtos::Stop::Time && r.idleSkips() == 3 && m.pc() == work);
        uint32_t d0 = 0;
        check("legacy borrowed call retains stock return behavior", r.callAsMain(borrowed, {}, d0) && d0 == 42 && m.pc() == ot::g_mainSpin);
    }
    {
        ot::Machine m(std::vector<uint8_t>{}); seed(m);
        m.write16(ot::g_mainSpin - 6, 0x4eb9); m.setPC(ot::g_mainSpin);
        ot::Rtos r(m); r.install(); timer(m);
        check("default stock spin still parks", r.runToMainSpin() == ot::Rtos::Stop::Gate);
        check("default stock idle advances timer without work", r.run(12, false) == ot::Rtos::Stop::Time && r.idleSkips() == 3 && m.pc() == ot::g_mainSpin && m.peek32(count) == 0);
        uint32_t d0 = 0;
        check("default borrowed call still returns stock spin", r.callAsMain(borrowed, {}, d0) && d0 == 42 && m.pc() == ot::g_mainSpin);
    }
    {
        ot::Machine m(std::vector<uint8_t>{}); seed(m);
        ot::Rtos r(m);
        check("zero rejected", !r.setMainPark(0, work));
        check("odd rejected", !r.setMainPark(park + 1, work));
        check("overlapping PC rejected", !r.setMainPark(park, park));
        check("overlapping alias rejected", !r.setMainPark(park, park + 0x08000000));
        check("unmapped rejected without growth", !r.setMainPark(0x60000000, work) && m.autoMappedPages() == 0);
        check("invalid resume rejected", !r.setMainPark(park, 0x60000000));
        check("invalid configuration preserves stock park", r.atSpin(ot::g_mainSpin));
    }
    return failures ? 1 : 0;
}
