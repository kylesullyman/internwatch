"""Technical terms used for two things:

1. Fabrication guard — if a rewritten bullet names a technology that the original entry
   never mentioned, the rewrite is rejected and the original bullet is kept.
2. JD keyword coverage — which of the JD's technical terms your resume actually contains.

Add to this list freely; it only needs to be the terms you'd expect in SWE/graphics/ML JDs.
"""
from __future__ import annotations

import re

TECH_TERMS = """
python c++ c c# java javascript typescript go golang rust kotlin swift scala ruby php lua r matlab julia
sql nosql bash shell powershell assembly mips x86 arm risc-v verilog vhdl systemverilog hlsl glsl cuda opencl
triton wgsl
react vue angular svelte next.js node.js node express django flask fastapi spring rails .net asp.net
unity unreal unreal engine godot blender maya houdini three.js webgl webgpu opengl vulkan directx direct3d metal
dx12 ray tracing path tracing rasterization shaders shader compute shaders rendering pipeline
pytorch tensorflow jax keras scikit-learn sklearn numpy pandas scipy opencv hugging face transformers
llm llms rag langchain llamaindex vector database embeddings fine-tuning reinforcement learning
computer vision nlp deep learning machine learning neural networks cnn transformer diffusion
tensorrt onnx cudnn nccl mpi openmp simd avx
docker kubernetes k8s terraform ansible aws gcp azure lambda s3 ec2 serverless ci/cd github actions jenkins
git linux unix windows macos ios android
postgresql postgres mysql sqlite mongodb redis cassandra dynamodb elasticsearch kafka rabbitmq celery
neo4j pinecone graphql rest grpc protobuf websockets microservices
spark hadoop airflow dbt snowflake bigquery databricks
qt imgui wxwidgets tkinter pyqt pyside
serial uart spi i2c can usb embedded firmware rtos fpga microcontroller arduino raspberry pi
gpu multithreading concurrency distributed systems networking tcp udp http
profiling nsight renderdoc pix gdb valgrind cmake bazel make
pytest junit gtest unit testing
figma photoshop
logisim
obsidian mcp
optics photonics polarimetry lasers labview
bioinformatics computational biology genomics proteomics transcriptomics single-cell scrna-seq
crispr pcr fasta fastq bam vcf blast biopython bioconductor samtools bwa rdkit openbabel pymol
alphafold rosetta esm protein folding protein design molecular dynamics gromacs openmm autodock
docking cheminformatics smiles admet qsar scanpy anndata seurat plink nextflow snakemake cwl
benchling lims flow cytometry mass spectrometry microscopy segmentation cellprofiler imagej napari
houdini vex maya mel openusd alembic substance designer substance painter nuke katana renderman arnold
speedtree ziva marvelous designer shotgrid pyside2 rigging skinning lookdev
""".split("\n")

# multi-word phrases stay intact; single words split out
_TERMS: set[str] = set()
for line in TECH_TERMS:
    line = line.strip()
    if not line:
        continue
    for multi in re.findall(r"(unreal engine|ray tracing|path tracing|compute shaders|rendering pipeline|"
                            r"hugging face|vector database|reinforcement learning|computer vision|deep learning|"
                            r"machine learning|neural networks|github actions|distributed systems|unit testing|"
                            r"raspberry pi|asp\.net|"
                            r"computational biology|protein folding|protein design|molecular dynamics|"
                            r"mass spectrometry|flow cytometry|substance designer|substance painter|"
                            r"marvelous designer)", line):
        _TERMS.add(multi)
        line = line.replace(multi, " ")
    _TERMS.update(w for w in line.split() if w)

# Ambiguous as plain English; only counted in the fabrication guard when capitalized in the text.
AMBIGUOUS = {"c", "r", "go", "rest", "make", "spark", "express", "metal", "node", "shell", "can", "lambda",
             # bio/VFX terms that are also ordinary words: only count as SMILES, BLAST, BAM, MEL, Nuke...
             "smiles", "blast", "bam", "esm", "mel", "vex", "nuke", "katana", "arnold", "alembic"}


def term_regex(term: str) -> re.Pattern:
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9+#])", re.I)


_REGEX = {t: term_regex(t) for t in _TERMS}


def terms_in(text: str, extra: set[str] | None = None, strict: bool = False) -> set[str]:
    """Return known technical terms mentioned in `text` (lowercased).

    strict=True skips ambiguous words (c, go, rest...) unless they appear with the exact
    casing you'd use for the technology (C, Go, REST...).
    """
    found = set()
    for t, rx in _REGEX.items():
        if strict and t in AMBIGUOUS:
            if not re.search(r"(?<![A-Za-z0-9])(" + re.escape(t.upper()) + "|" + re.escape(t.capitalize())
                             + r")(?![A-Za-z0-9+#])", text):
                continue
        if rx.search(text):
            found.add(t)
    for t in extra or ():
        if term_regex(t).search(text):
            found.add(t.lower())
    return found
