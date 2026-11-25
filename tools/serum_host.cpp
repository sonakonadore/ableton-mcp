#include <CoreFoundation/CoreFoundation.h>
#include <cstdio>
#include <fstream>
#include "pluginterfaces/vst/ivstprocesscontext.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <vector>
#include "pluginterfaces/base/ibstream.h"
#include "pluginterfaces/vst/ivstevents.h"
#include "pluginterfaces/vst/vstspeaker.h"

#include <cstring>
#include <string>
#include "pluginterfaces/base/ipluginbase.h"
#include "pluginterfaces/vst/ivstcomponent.h"
#include "pluginterfaces/vst/ivstaudioprocessor.h"
#include "pluginterfaces/vst/ivsteditcontroller.h"
#include "pluginterfaces/vst/ivsthostapplication.h"

using namespace Steinberg;
using namespace Steinberg::Vst;

class ProbeHost final : public IHostApplication {
    uint32 refs = 1;
public:
    tresult PLUGIN_API queryInterface(const TUID iid, void **object) override {
        *object = nullptr;
        if (std::memcmp(iid, INLINE_UID_OF(IHostApplication), 16) &&
            std::memcmp(iid, INLINE_UID_OF(FUnknown), 16)) return kNoInterface;
        *object = static_cast<IHostApplication *>(this);
        addRef();
        return kResultOk;
    }
    uint32 PLUGIN_API addRef() override { return ++refs; }
    uint32 PLUGIN_API release() override { return --refs; }
    tresult PLUGIN_API getName(String128 name) override {
        constexpr char16_t label[] = u"Local VST3 Host Probe";
        std::memcpy(name, label, sizeof(label));
        return kResultOk;
    }
    tresult PLUGIN_API createInstance(TUID, TUID, void **object) override {
        *object = nullptr;
        return kNoInterface;
    }
};
template<class Base>
class ProbeInterface : public Base {
    uint32 refs = 1;

public:
    tresult PLUGIN_API queryInterface(const TUID iid, void **object) override {
        *object = nullptr;
        if (std::memcmp(iid, std::is_same_v<Base, IBStream> ? INLINE_UID_OF(IBStream) : INLINE_UID_OF(IEventList), 16)
            && std::memcmp(iid, INLINE_UID_OF(FUnknown), 16)) return kNoInterface;
        *object = static_cast<Base *>(this);
        addRef();
        return kResultOk;
    }
    uint32 PLUGIN_API addRef() override { return ++refs; }
    uint32 PLUGIN_API release() override { return --refs; }
};

class ProbeStream final : public ProbeInterface<IBStream> {
    static constexpr int64 limit = 16 * 1024 * 1024;
    int64 cursor = 0;

public:
    std::vector<char> data;

    tresult PLUGIN_API read(void *buffer, int32 bytes, int32 *readBytes) override {
        if (bytes < 0) return kInvalidArgument;
        auto count = std::min<int64>(bytes, std::max<int64>(0, size() - cursor));
        if (count > 0) std::memcpy(buffer, data.data() + cursor, count);
        cursor += count;
        if (readBytes) *readBytes = static_cast<int32>(count);
        return kResultOk;
    }
    tresult PLUGIN_API write(void *buffer, int32 bytes, int32 *written) override {
        if (bytes < 0 || cursor + bytes > limit) return kInvalidArgument;
        if (cursor + bytes > size()) data.resize(cursor + bytes);
        if (bytes > 0) std::memcpy(data.data() + cursor, buffer, bytes);
        cursor += bytes;
        if (written) *written = bytes;
        return kResultOk;
    }
    tresult PLUGIN_API seek(int64 offset, int32 mode, int64 *newPosition) override {
        if (offset < -limit || offset > limit) return kInvalidArgument;
        if (mode == kIBSeekCur) offset += cursor;
        if (mode == kIBSeekEnd) offset += size();
        if (mode < kIBSeekSet || mode > kIBSeekEnd || offset < 0 || offset > limit) return kInvalidArgument;
        cursor = offset;
        if (newPosition) *newPosition = cursor;
        return kResultOk;
    }
    tresult PLUGIN_API tell(int64 *position) override {
        if (!position) return kInvalidArgument;
        *position = cursor;
        return kResultOk;
    }
    int64 size() const { return static_cast<int64>(data.size()); }
};

class ProbeEvents final : public ProbeInterface<IEventList> {
public:
    std::vector<Event> events;
    int32 PLUGIN_API getEventCount() override { return static_cast<int32>(events.size()); }
    tresult PLUGIN_API getEvent(int32 index, Event &event) override {
        if (index < 0 || index >= getEventCount()) return kInvalidArgument;
        event = events[index];
        return kResultOk;
    }
    tresult PLUGIN_API addEvent(Event &event) override {
        if (events.size() >= 1000) return kResultFalse;
        events.push_back(event);
        return kResultOk;
    }
};

constexpr int blockSize = 256;
constexpr int sampleRate = 48000;
constexpr int blocks = 375;
constexpr int maxFrames = 30 * sampleRate;
constexpr double twoPi = 6.283185307179586;
using Frame = std::array<Sample32, blockSize>;

struct ScheduledEvent {
    int sample;
    Event event;
};

struct Audition {
    int frames = blockSize * blocks;
    double tempo = 120;
    bool specified = false;
    std::vector<ScheduledEvent> notes;
    std::vector<Sample32> input;
};

bool readAudition(Audition &audition, const char *path) {
    std::ifstream file(path);
    int count = 0;
    if (!(file >> audition.frames >> audition.tempo >> count)
        || audition.frames < 1 || audition.frames > maxFrames
        || !std::isfinite(audition.tempo) || audition.tempo < 20 || audition.tempo > 300
        || count < 0 || count > 256) return false;
    audition.specified = true;
    for (int index = 0; index < count; ++index) {
        int start, end, pitch, channel;
        double velocity;
        if (!(file >> start >> end >> pitch >> velocity >> channel)
            || start < 0 || end <= start || end >= audition.frames || pitch < 0 || pitch > 127
            || !std::isfinite(velocity) || velocity <= 0 || velocity > 1 || channel < 0 || channel > 15) return false;
        Event on{};
        on.type = Event::kNoteOnEvent;
        on.noteOn.channel = channel;
        on.noteOn.pitch = pitch;
        on.noteOn.velocity = velocity;
        on.noteOn.noteId = index + 1;
        Event off{};
        off.type = Event::kNoteOffEvent;
        off.noteOff.channel = channel;
        off.noteOff.pitch = pitch;
        off.noteOff.noteId = index + 1;
        audition.notes.push_back({start, on});
        audition.notes.push_back({end, off});
    }
    std::stable_sort(audition.notes.begin(), audition.notes.end(), [](const auto &a, const auto &b) {
        return a.sample < b.sample || (a.sample == b.sample && a.event.type == Event::kNoteOffEvent
                                     && b.event.type == Event::kNoteOnEvent);
    });
    std::string trailing;
    return !(file >> trailing);
}

bool readAudioInput(Audition &audition, const char *path) {
    std::ifstream file(path, std::ios::binary | std::ios::ate);
    const auto size = file.tellg();
    constexpr int bytesPerFrame = sizeof(Sample32) * 2;
    if (!file || size <= 0 || size % bytesPerFrame || size > audition.frames * bytesPerFrame) return false;
    audition.input.resize(size / sizeof(Sample32));
    file.seekg(0);
    return file.read(reinterpret_cast<char *>(audition.input.data()), size).good()
        && std::all_of(audition.input.begin(), audition.input.end(), [](Sample32 x) { return std::isfinite(x); });
}

bool render(IComponent *component, bool effect, const char *output, const Audition &audition) {
    IAudioProcessor *processor = nullptr;
    if (component->queryInterface(INLINE_UID_OF(IAudioProcessor), reinterpret_cast<void **>(&processor)) != kResultOk)
        return false;

    SpeakerArrangement stereo = SpeakerArr::kStereo;
    ProcessSetup setup{kRealtime, kSample32, blockSize, sampleRate};
    const bool ready = processor->setBusArrangements(effect ? &stereo : nullptr, effect ? 1 : 0, &stereo, 1) == kResultOk
        && (!effect || component->activateBus(kAudio, kInput, 0, true) == kResultOk)
        && component->activateBus(kAudio, kOutput, 0, true) == kResultOk
        && component->activateBus(kEvent, kInput, 0, true) == kResultOk
        && processor->setupProcessing(setup) == kResultOk
        && component->setActive(true) == kResultOk;

    bool success = false;
    if (ready && processor->setProcessing(true) == kResultOk) {
        Frame leftIn{}, rightIn{}, leftOut{}, rightOut{};
        Sample32 *ins[] = {leftIn.data(), rightIn.data()};
        Sample32 *outs[] = {leftOut.data(), rightOut.data()};
        AudioBusBuffers input{}, outputBus{};
        input.numChannels = outputBus.numChannels = 2;
        input.channelBuffers32 = ins;
        outputBus.channelBuffers32 = outs;

        ProbeEvents events;
        ProcessContext context{};
        context.state = ProcessContext::kPlaying | ProcessContext::kTempoValid
            | ProcessContext::kProjectTimeMusicValid | ProcessContext::kTimeSigValid;
        context.sampleRate = sampleRate;
        context.tempo = audition.tempo;
        context.timeSigNumerator = 4;
        context.timeSigDenominator = 4;

        ProcessData frame{};
        frame.processMode = kRealtime;
        frame.symbolicSampleSize = kSample32;
        frame.numInputs = effect ? 1 : 0;
        frame.numOutputs = 1;
        frame.inputs = effect ? &input : nullptr;
        frame.outputs = &outputBus;
        frame.inputEvents = &events;
        frame.processContext = &context;

        std::vector<Sample32> audio;
        audio.reserve(audition.frames * 2);
        std::size_t noteIndex = 0;
        for (int start = 0; start < audition.frames; start += blockSize) {
            frame.numSamples = std::min(blockSize, audition.frames - start);
            context.projectTimeSamples = start;
            context.projectTimeMusic = start * audition.tempo / (60.0 * sampleRate);
            events.events.clear();
            if (effect) {
                input.silenceFlags = 3;
                for (int i = 0; i < frame.numSamples; ++i) {
                    const auto index = static_cast<std::size_t>(start + i) * 2;
                    if (audition.input.empty()) {
                        const double t = (start + i) / double(sampleRate);
                        const auto signal = t < 0.8 ? std::sin(twoPi * 82 * t) * 0.14
                            + std::sin(twoPi * 431 * t) * 0.12 + std::sin(twoPi * 2101 * t) * 0.075 : 0.0;
                        leftIn[i] = signal;
                        rightIn[i] = signal * 0.85f;
                    } else {
                        leftIn[i] = index < audition.input.size() ? audition.input[index] : 0;
                        rightIn[i] = index < audition.input.size() ? audition.input[index + 1] : 0;
                    }
                    if (leftIn[i] != 0) input.silenceFlags &= ~1ull;
                    if (rightIn[i] != 0) input.silenceFlags &= ~2ull;
                }
            }
            if (audition.specified) {
                while (noteIndex < audition.notes.size() && audition.notes[noteIndex].sample < start + frame.numSamples) {
                    const auto &scheduled = audition.notes[noteIndex++];
                    auto event = scheduled.event;
                    event.sampleOffset = scheduled.sample - start;
                    event.ppqPosition = scheduled.sample * audition.tempo / (60.0 * sampleRate);
                    events.addEvent(event);
                }
            } else if (!effect && (start == 0 || start == 188 * blockSize)) {
                Event event{};
                if (start == 0) {
                    event.type = Event::kNoteOnEvent;
                    event.noteOn.pitch = 48;
                    event.noteOn.velocity = 0.75;
                    event.noteOn.noteId = 1;
                } else {
                    event.type = Event::kNoteOffEvent;
                    event.noteOff.pitch = 48;
                    event.noteOff.noteId = 1;
                }
                events.addEvent(event);
            }
            leftOut.fill(0);
            rightOut.fill(0);
            outputBus.silenceFlags = 0;
            if (processor->process(frame) != kResultOk)
                break;
            for (int i = 0; i < frame.numSamples; ++i) {
                audio.push_back(leftOut[i]);
                audio.push_back(rightOut[i]);
            }
        }
        processor->setProcessing(false);
        success = audio.size() == static_cast<std::size_t>(audition.frames) * 2
            && std::all_of(audio.begin(), audio.end(), [](Sample32 x) { return std::isfinite(x); });
        if (success) {
            std::ofstream file(output, std::ios::binary);
            file.write(reinterpret_cast<const char *>(audio.data()), audio.size() * sizeof(Sample32));
            success = file.good();
        }
    }
    if (ready)
        component->setActive(false);
    processor->release();
    return success;
}

bool inputState(ProbeStream &stream, const char *path) {
    std::ifstream input(path, std::ios::binary | std::ios::ate);
    const auto size = input.tellg();
    if (!input.good() || size <= 0 || size > 16 * 1024 * 1024)
        return false;
    stream.data.resize(size);
    input.seekg(0);
    input.read(stream.data.data(), size);
    return input.good();
}

int main(int argc, char **argv) {
    if (argc < 6 || (std::strcmp(argv[2], "instrument") && std::strcmp(argv[2], "fx"))) {
        std::fprintf(stderr, "usage: serum_host PLUGIN_BUNDLE instrument|fx INPUT_STATE|- OUTPUT_STATE OUTPUT_F32LE|- "
            "[--class-name VST3_CLASS] [--wait-for-samples] [--audition EVENTS] [--audio-input STEREO_F32LE]\n");
        return 1;
    }
    const bool effect = !std::strcmp(argv[2], "fx");
    const char *eventsPath = nullptr, *audioPath = nullptr;
    const char *className = nullptr;
    bool waitForSamples = false;
    for (int index = 6; index < argc; ++index) {
        if (!std::strcmp(argv[index], "--wait-for-samples") && !waitForSamples) waitForSamples = true;
        else if (!std::strcmp(argv[index], "--class-name") && !className && index + 1 < argc) className = argv[++index];
        else if (!std::strcmp(argv[index], "--audition") && !eventsPath && index + 1 < argc) eventsPath = argv[++index];
        else if (!std::strcmp(argv[index], "--audio-input") && !audioPath && index + 1 < argc) audioPath = argv[++index];
        else { std::fputs("Invalid serum_host option.\n", stderr); return 1; }
    }
    Audition audition;
    if ((eventsPath && !readAudition(audition, eventsPath))
        || (audioPath && (!effect || !eventsPath || !readAudioInput(audition, audioPath)))) {
        std::fputs("Invalid Serum audition input.\n", stderr);
        return 1;
    }
    auto path = reinterpret_cast<const UInt8 *>(argv[1]);
    auto url = CFURLCreateFromFileSystemRepresentation(nullptr, path, std::strlen(argv[1]), true);
    if (!url) return 2;
    auto bundle = CFBundleCreate(nullptr, url);
    CFRelease(url);
    if (!bundle || !CFBundleLoadExecutable(bundle)) return 2;
    auto entry = reinterpret_cast<bool (*)(CFBundleRef)>(CFBundleGetFunctionPointerForName(bundle, CFSTR("bundleEntry")));
    auto leave = reinterpret_cast<bool (*)()>(CFBundleGetFunctionPointerForName(bundle, CFSTR("bundleExit")));
    auto makeFactory = reinterpret_cast<IPluginFactory *(*)()>(CFBundleGetFunctionPointerForName(bundle, CFSTR("GetPluginFactory")));
    if (!entry || !leave || !makeFactory || !entry(bundle)) return 2;
    auto factory = makeFactory();
    if (!factory) return 2;

    ProbeHost host;
    IComponent *component = nullptr;
    const char *name = className ? className : (effect ? "Serum 2 FX" : "Serum 2");
    for (int32 index = 0; index < factory->countClasses(); ++index) {
        PClassInfo item{};
        if (factory->getClassInfo(index, &item) != kResultOk || std::strcmp(item.name, name)
            || std::strcmp(item.category, kVstAudioEffectClass)) continue;
        factory->createInstance(item.cid, INLINE_UID_OF(IComponent), reinterpret_cast<void **>(&component));
        break;
    }
    int result = 3;
    if (component && component->initialize(&host) == kResultOk) {
        result = 4;
        ProbeStream before, after;
        bool loaded = !std::strcmp(argv[3], "-")
            || (inputState(before, argv[3]) && component->setState(&before) == kResultOk);
        if (loaded && waitForSamples)
            CFRunLoopRunInMode(kCFRunLoopDefaultMode, 1.5, false);
        if (loaded && component->getState(&after) == kResultOk && after.size() > 0) {
            if (!std::strcmp(argv[5], "-") || render(component, effect, argv[5], audition)) {
                std::ofstream file(argv[4], std::ios::binary);
                file.write(after.data.data(), after.size());
                result = file.good() ? 0 : 5;
            } else {
                result = 6;
            }
        }
        component->terminate();
    }
    if (component) component->release();
    factory->release();
    leave();
    CFRelease(bundle);
    if (result) std::fprintf(stderr, "Serum host probe failed at stage %d\n", result);
    return result;
}
