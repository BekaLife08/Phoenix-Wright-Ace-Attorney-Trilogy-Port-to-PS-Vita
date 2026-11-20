// Port-owned encoder for the backwards-compatible predictor subset of HEVAG.
// Reads PCM16 WAV, writes 16-byte/28-sample channel-interleaved ADPCM frames.
// No game samples, Sony SDK code or lookup tables are embedded.
using System;
using System.IO;

public static class HevagEncoder
{
    static readonly float[] C1={0f,60f/64f,115f/64f,98f/64f,122f/64f};
    static readonly float[] C2={0f,0f,-52f/64f,-55f/64f,-60f/64f};
    static int Clamp(int v){return Math.Max(-32768,Math.Min(32767,v));}

    static short[] ReadWave(string path,out int channels,out int rate)
    {
        channels=rate=0;byte[] pcm=null;int format=0,bits=0;
        using(var reader=new BinaryReader(File.OpenRead(path))) {
            if(new string(reader.ReadChars(4))!="RIFF")throw new Exception("Expected RIFF WAV");
            reader.ReadUInt32();if(new string(reader.ReadChars(4))!="WAVE")throw new Exception("Expected WAVE");
            while(reader.BaseStream.Position+8<=reader.BaseStream.Length) {
                string id=new string(reader.ReadChars(4));uint size=reader.ReadUInt32();long next=reader.BaseStream.Position+size;
                if(next>reader.BaseStream.Length)throw new Exception("Truncated WAV");
                if(id=="fmt ") {format=reader.ReadUInt16();channels=reader.ReadUInt16();rate=reader.ReadInt32();reader.ReadInt32();reader.ReadUInt16();bits=reader.ReadUInt16();}
                if(id=="data")pcm=reader.ReadBytes(checked((int)size));
                reader.BaseStream.Position=next+(size&1);
            }
        }
        if(format!=1||bits!=16||channels<1||channels>8||rate<=0||pcm==null||pcm.Length%(2*channels)!=0)throw new Exception("Expected PCM16 WAV");
        short[] samples=new short[pcm.Length/2];Buffer.BlockCopy(pcm,0,samples,0,pcm.Length);return samples;
    }

    public static void Main(string[] args)
    {
        if(args.Length!=2)throw new Exception("Usage: HevagEncoder input.wav output.raw");
        int channels,rate;short[] pcm=ReadWave(args[0],out channels,out rate);int count=pcm.Length/channels;
        int[] h1=new int[channels],h2=new int[channels],values=new int[28];
        sbyte[] candidate=new sbyte[28],best=new sbyte[28];
        using(var writer=new BinaryWriter(new FileStream(args[1],FileMode.CreateNew,FileAccess.Write))) {
            for(int offset=0;offset<count;offset+=28)for(int channel=0;channel<channels;channel++) {
                for(int i=0;i<28;i++)values[i]=offset+i<count?pcm[(offset+i)*channels+channel]:0;
                long bestError=long.MaxValue;int bestFilter=0,bestShift=0,bestH1=0,bestH2=0;
                for(int filter=0;filter<5;filter++) {
                    int a=h1[channel],b=h2[channel],peak=0;
                    for(int i=0;i<28;i++){int pred=(int)(a*C1[filter]+b*C2[filter]);peak=Math.Max(peak,Math.Abs(values[i]-pred));b=a;a=values[i];}
                    int estimate=0;while(estimate<12&&peak<=(7*(4096>>(estimate+1))))estimate++;
                    for(int shift=Math.Max(0,estimate-1);shift<=Math.Min(12,estimate+1);shift++) {
                        int step=4096>>shift;a=h1[channel];b=h2[channel];long error=0;
                        for(int i=0;i<28;i++) {
                            int pred=(int)(a*C1[filter]+b*C2[filter]);
                            int nibble=(int)Math.Round((values[i]-pred)/(double)step,MidpointRounding.AwayFromZero);
                            nibble=Math.Max(-8,Math.Min(7,nibble));candidate[i]=(sbyte)nibble;
                            int decoded=pred+nibble*step;long difference=values[i]-Clamp(decoded);error+=difference*difference;b=a;a=decoded;
                        }
                        if(error<bestError){bestError=error;bestFilter=filter;bestShift=shift;bestH1=a;bestH2=b;Array.Copy(candidate,best,28);}
                    }
                }
                writer.Write((byte)((bestFilter<<4)|bestShift));writer.Write((byte)0);
                for(int i=0;i<28;i+=2)writer.Write((byte)((best[i]&15)|((best[i+1]&15)<<4)));
                h1[channel]=bestH1;h2[channel]=bestH2;
            }
        }
        Console.WriteLine("Encoded "+count+" samples, "+channels+" channels, "+rate+" Hz");
    }
}
