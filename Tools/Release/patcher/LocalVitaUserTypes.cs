using System;
using System.IO;
using Mono.Cecil;

public static class LocalVitaUserTypes
{
    private static void WriteType(TextWriter writer, string module, TypeDefinition type)
    {
        if (type.Name != "<Module>")
            writer.WriteLine(module + "\t" + type.FullName.Replace('/', '+'));
        foreach (TypeDefinition nested in type.NestedTypes)
            WriteType(writer, module, nested);
    }

    public static void Main(string[] args)
    {
        using (var output = new StreamWriter(args[0]))
            for (int index = 1; index < args.Length; index++)
            {
                var module = AssemblyDefinition.ReadAssembly(args[index]).MainModule;
                foreach (TypeDefinition type in module.Types)
                    WriteType(output, Path.GetFileName(args[index]), type);
            }
    }
}
