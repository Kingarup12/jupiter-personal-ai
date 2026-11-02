using System;
using System.IO;
using System.Runtime.InteropServices;
using System.Threading;

namespace Jupiter.SystemAudio
{
    internal static class Program
    {
        private const string ProcessLoopbackDevice = "VAD\\Process_Loopback";
        private const ushort VtBlob = 65;
        private const uint StreamLoopback = 0x00020000;
        private const uint StreamEventCallback = 0x00040000;
        private const uint StreamAutoConvertPcm = 0x80000000;
        private const uint StreamSrcDefaultQuality = 0x08000000;
        private const uint BufferSilent = 0x00000002;
        private static readonly Guid AudioClientGuid = new Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2");
        private static readonly Guid AudioCaptureClientGuid = new Guid("C8ADBD64-E71E-48a0-A4DE-185C395CD317");
        private static string Stage = "startup";

        [STAThread]
        private static int Main(string[] args)
        {
            uint rootPid;
            if (args.Length != 1 || !UInt32.TryParse(args[0], out rootPid) || rootPid == 0) return 2;
            try
            {
                return Capture(rootPid);
            }
            catch (Exception error)
            {
                Console.Error.WriteLine(Stage + ": " + error.ToString());
                return 3;
            }
        }

        private static int Capture(uint rootPid)
        {
            var activation = new AudioClientActivationParams {
                ActivationType = 1,
                ProcessLoopbackParams = new ProcessLoopbackParams {
                    TargetProcessId = rootPid,
                    ProcessLoopbackMode = 1
                }
            };
            IntPtr activationBlob = Marshal.AllocCoTaskMem(Marshal.SizeOf(typeof(AudioClientActivationParams)));
            IntPtr propVariant = Marshal.AllocCoTaskMem(Marshal.SizeOf(typeof(PropVariant)));
            try
            {
                Marshal.StructureToPtr(activation, activationBlob, false);
                Marshal.StructureToPtr(new PropVariant {
                    VariantType = VtBlob,
                    Blob = new Blob {
                        Size = Marshal.SizeOf(typeof(AudioClientActivationParams)),
                        Data = activationBlob
                    }
                }, propVariant, false);

                using (var completion = new UnmanagedActivationHandler())
                {
                    IActivateAudioInterfaceAsyncOperation operation;
                    var audioClientId = AudioClientGuid;
                    Stage = "activate";
                    ThrowIfFailed(ActivateAudioInterfaceAsync(
                        ProcessLoopbackDevice, ref audioClientId, propVariant, completion.Pointer, out operation));
                    if (!completion.Completed.WaitOne(8000)) return 4;
                    Stage = "activation-result";
                    ThrowIfFailed(completion.ActivationResult);
                    var audioClient = (IAudioClient)completion.ActivatedInterface;
                var format = new WaveFormatEx {
                    FormatTag = 1,
                    Channels = 1,
                    SamplesPerSecond = 16000,
                    AverageBytesPerSecond = 32000,
                    BlockAlign = 2,
                    BitsPerSample = 16,
                    ExtraSize = 0
                };
                Stage = "initialize-audio-client";
                ThrowIfFailed(audioClient.Initialize(
                    0,
                    StreamLoopback | StreamEventCallback | StreamAutoConvertPcm | StreamSrcDefaultQuality,
                    0,
                    0,
                    ref format,
                    IntPtr.Zero));
                using (var audioReady = new AutoResetEvent(false))
                using (var stopping = new ManualResetEvent(false))
                {
                    Stage = "set-event-handle";
                    ThrowIfFailed(audioClient.SetEventHandle(audioReady.SafeWaitHandle.DangerousGetHandle()));
                    object captureObject;
                    var captureClientId = AudioCaptureClientGuid;
                    Stage = "get-capture-service";
                    ThrowIfFailed(audioClient.GetService(ref captureClientId, out captureObject));
                    var captureClient = (IAudioCaptureClient)captureObject;
                    var stopThread = new Thread(() => {
                        try
                        {
                            var input = Console.OpenStandardInput();
                            while (input.ReadByte() >= 0) { }
                        }
                        catch { }
                        stopping.Set();
                    });
                    stopThread.IsBackground = true;
                    stopThread.Start();
                    Stage = "start-audio-client";
                    ThrowIfFailed(audioClient.Start());
                    var output = Console.OpenStandardOutput();
                    var waits = new WaitHandle[] { stopping, audioReady };
                    while (WaitHandle.WaitAny(waits) == 1)
                    {
                        DrainPackets(captureClient, output);
                    }
                    audioClient.Stop();
                }
                }
                return 0;
            }
            finally
            {
                Marshal.FreeCoTaskMem(propVariant);
                Marshal.FreeCoTaskMem(activationBlob);
            }
        }

        private static void DrainPackets(IAudioCaptureClient capture, Stream output)
        {
            uint nextFrames;
            ThrowIfFailed(capture.GetNextPacketSize(out nextFrames));
            while (nextFrames > 0)
            {
                IntPtr data;
                uint frames;
                uint flags;
                ulong devicePosition;
                ulong qpcPosition;
                ThrowIfFailed(capture.GetBuffer(out data, out frames, out flags, out devicePosition, out qpcPosition));
                int byteCount = checked((int)frames * 2);
                var bytes = new byte[byteCount];
                if ((flags & BufferSilent) == 0 && data != IntPtr.Zero) Marshal.Copy(data, bytes, 0, byteCount);
                output.Write(bytes, 0, bytes.Length);
                output.Flush();
                ThrowIfFailed(capture.ReleaseBuffer(frames));
                ThrowIfFailed(capture.GetNextPacketSize(out nextFrames));
            }
        }

        private static void ThrowIfFailed(int hresult)
        {
            if (hresult < 0) Marshal.ThrowExceptionForHR(hresult);
        }

        [DllImport("Mmdevapi.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
        private static extern int ActivateAudioInterfaceAsync(
            string deviceInterfacePath,
            ref Guid interfaceId,
            IntPtr activationParams,
            IntPtr completionHandler,
            out IActivateAudioInterfaceAsyncOperation activationOperation);

        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessLoopbackParams
        {
            public uint TargetProcessId;
            public int ProcessLoopbackMode;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct AudioClientActivationParams
        {
            public int ActivationType;
            public ProcessLoopbackParams ProcessLoopbackParams;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct Blob
        {
            public int Size;
            public IntPtr Data;
        }

        [StructLayout(LayoutKind.Explicit)]
        private struct PropVariant
        {
            [FieldOffset(0)] public ushort VariantType;
            [FieldOffset(8)] public Blob Blob;
        }

        [StructLayout(LayoutKind.Sequential, Pack = 2)]
        private struct WaveFormatEx
        {
            public ushort FormatTag;
            public ushort Channels;
            public uint SamplesPerSecond;
            public uint AverageBytesPerSecond;
            public ushort BlockAlign;
            public ushort BitsPerSample;
            public ushort ExtraSize;
        }

        [ComImport, Guid("72A22D78-CDE4-431D-B8CC-843A71199B6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IActivateAudioInterfaceAsyncOperation
        {
            [PreserveSig] int GetActivateResult(out int activateResult, [MarshalAs(UnmanagedType.IUnknown)] out object activatedInterface);
        }

        private sealed class UnmanagedActivationHandler : IDisposable
        {
            private static readonly Guid IUnknownId = new Guid("00000000-0000-0000-C000-000000000046");
            private static readonly Guid CompletionHandlerId = new Guid("41D949AB-9862-444A-80F6-C261334DA5EB");
            private static readonly Guid AgileObjectId = new Guid("94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90");
            [UnmanagedFunctionPointer(CallingConvention.StdCall)]
            private delegate int QueryInterfaceDelegate(IntPtr self, ref Guid interfaceId, out IntPtr result);
            [UnmanagedFunctionPointer(CallingConvention.StdCall)]
            private delegate uint RefDelegate(IntPtr self);
            [UnmanagedFunctionPointer(CallingConvention.StdCall)]
            private delegate int ActivateCompletedDelegate(IntPtr self, IntPtr operation);
            private static readonly QueryInterfaceDelegate QueryInterfaceCallback = QueryInterface;
            private static readonly RefDelegate AddRefCallback = AddRef;
            private static readonly RefDelegate ReleaseCallback = Release;
            private static readonly ActivateCompletedDelegate ActivateCompletedCallback = ActivateCompleted;

            internal readonly AutoResetEvent Completed = new AutoResetEvent(false);
            internal int ActivationResult = unchecked((int)0x80004005);
            internal object ActivatedInterface;
            internal readonly IntPtr Pointer;
            private readonly IntPtr _vtable;
            private readonly GCHandle _selfHandle;
            private int _references = 1;

            internal UnmanagedActivationHandler()
            {
                _selfHandle = GCHandle.Alloc(this);
                _vtable = Marshal.AllocCoTaskMem(IntPtr.Size * 4);
                Marshal.WriteIntPtr(_vtable, IntPtr.Size * 0, Marshal.GetFunctionPointerForDelegate(QueryInterfaceCallback));
                Marshal.WriteIntPtr(_vtable, IntPtr.Size * 1, Marshal.GetFunctionPointerForDelegate(AddRefCallback));
                Marshal.WriteIntPtr(_vtable, IntPtr.Size * 2, Marshal.GetFunctionPointerForDelegate(ReleaseCallback));
                Marshal.WriteIntPtr(_vtable, IntPtr.Size * 3, Marshal.GetFunctionPointerForDelegate(ActivateCompletedCallback));
                Pointer = Marshal.AllocCoTaskMem(IntPtr.Size * 2);
                Marshal.WriteIntPtr(Pointer, 0, _vtable);
                Marshal.WriteIntPtr(Pointer, IntPtr.Size, GCHandle.ToIntPtr(_selfHandle));
            }

            private static UnmanagedActivationHandler Instance(IntPtr self)
            {
                return (UnmanagedActivationHandler)GCHandle.FromIntPtr(Marshal.ReadIntPtr(self, IntPtr.Size)).Target;
            }

            private static int QueryInterface(IntPtr self, ref Guid interfaceId, out IntPtr result)
            {
                if (interfaceId == IUnknownId || interfaceId == CompletionHandlerId || interfaceId == AgileObjectId)
                {
                    result = self;
                    AddRef(self);
                    return 0;
                }
                result = IntPtr.Zero;
                return unchecked((int)0x80004002);
            }

            private static uint AddRef(IntPtr self)
            {
                return (uint)Interlocked.Increment(ref Instance(self)._references);
            }

            private static uint Release(IntPtr self)
            {
                return (uint)Math.Max(0, Interlocked.Decrement(ref Instance(self)._references));
            }

            private static int ActivateCompleted(IntPtr self, IntPtr operationPointer)
            {
                var instance = Instance(self);
                IActivateAudioInterfaceAsyncOperation operation = null;
                try
                {
                    operation = (IActivateAudioInterfaceAsyncOperation)Marshal.GetObjectForIUnknown(operationPointer);
                    return operation.GetActivateResult(out instance.ActivationResult, out instance.ActivatedInterface);
                }
                finally
                {
                    if (operation != null && Marshal.IsComObject(operation)) Marshal.ReleaseComObject(operation);
                    instance.Completed.Set();
                }
            }

            public void Dispose()
            {
                Completed.Dispose();
                if (_selfHandle.IsAllocated) _selfHandle.Free();
                Marshal.FreeCoTaskMem(Pointer);
                Marshal.FreeCoTaskMem(_vtable);
            }
        }

        [ComImport, Guid("1CB9AD4C-DBFA-4c32-B178-C2F568A703B2"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IAudioClient
        {
            [PreserveSig] int Initialize(int shareMode, uint streamFlags, long bufferDuration, long periodicity, ref WaveFormatEx format, IntPtr sessionGuid);
            [PreserveSig] int GetBufferSize(out uint bufferFrames);
            [PreserveSig] int GetStreamLatency(out long latency);
            [PreserveSig] int GetCurrentPadding(out uint padding);
            [PreserveSig] int IsFormatSupported(int shareMode, ref WaveFormatEx format, IntPtr closestMatch);
            [PreserveSig] int GetMixFormat(out IntPtr format);
            [PreserveSig] int GetDevicePeriod(out long defaultPeriod, out long minimumPeriod);
            [PreserveSig] int Start();
            [PreserveSig] int Stop();
            [PreserveSig] int Reset();
            [PreserveSig] int SetEventHandle(IntPtr eventHandle);
            [PreserveSig] int GetService(ref Guid serviceId, [MarshalAs(UnmanagedType.IUnknown)] out object service);
        }

        [ComImport, Guid("C8ADBD64-E71E-48A0-A4DE-185C395CD317"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IAudioCaptureClient
        {
            [PreserveSig] int GetBuffer(out IntPtr data, out uint frames, out uint flags, out ulong devicePosition, out ulong qpcPosition);
            [PreserveSig] int ReleaseBuffer(uint frames);
            [PreserveSig] int GetNextPacketSize(out uint frames);
        }
    }
}
