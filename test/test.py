from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("NVIDIA_API_KEY")

client = OpenAI(
  base_url = "https://integrate.api.nvidia.com/v1",
  api_key = API_KEY
)

completion = client.chat.completions.create(
  model="meta/muse-glimmer-30b",
  messages=[{"role":"user","content":"Which number is larger, 9.11 or 9.8?"}],
  temperature=1,
  top_p=0.7,
  max_tokens=4096,
  stream=False
)

print(completion)
"""
ChatCompletion(id='acda5dbaff3c134b', 
               choices=[
                 Choice(finish_reason='stop', 
                        index=0, 
                        logprobs=None, 
                        message=ChatCompletionMessage(
                          content='9.8 is larger.\n\nWritten out to the same number of decimal places 9.8 = 9.80, and\n9.80 > 9.11.\n\nThe 8 is in the tenths place and the 1 is in the tenths place, so 8 tenths > 1 tenth. The 11 in the hundredths place does not make up the difference.', 
                          refusal=None, 
                          role='assistant', 
                          annotations=None, 
                          audio=None, 
                          function_call=None, 
                          tool_calls=None, 
                          reasoning_content='Which number is larger, 9.11 or 9.8?\n\nWhich number is larger, 9.11 or 9.8?\n\n9.11 vs 9.8\n\n9.11 > 9.8? Wait 9.11 = 9.11, 9.8 = 9.80. So 9.11 < 9.80.\n\nBut sometimes people think 9.11 > 9.8 because 11 > 8. Common misconception.\n\nSo answer: 9.8 is larger. 9.8 = 9.80 > 9.11.\n\nIf comparing as decimal numbers, 9.8 is larger.\n\nIf they mean 9.11 as 9.11 vs 9.8, yes 9.8 is larger.\n\nCould also note if they mean 9.11 as 9 point 11 vs 9 point 8.\n\nAnswer: 9.8 is larger.\n\nMight be trick about 9.11 > 9.8? No.\n\nThus answer: 9.8 is larger (9.80 > 9.11).\n\nIf they meant 9.11 as 9.11 (nine point eleven) vs 9.8 (nine point eight). 9.8 is larger.\n\nPossibly mention that 9.11 < 9.8.\n\nProvide explanation.\n\nFinal answer: 9.8 is larger.\n\nIf they think in terms of two-digit after decimal? No.\n\nAnswer succinctly.\n\nMaybe add: 9.8 = 9.80.\n\nSo 9.8 > 9.11.\n\nReturn.\n\nPotential nuance: Some might think 9.11 is larger because 11 > 8. But correct is 9.8.\n\nProvide answer.\n\nProbably want: 9.8 is larger.\n\nDone.'
                          )
                        )
               ], 
               created=1786449210, 
               model='meta/muse-glimmer-30b', 
               object='chat.completion', 
               moderation=None, 
               service_tier=None, 
               system_fingerprint=None, 
               usage=CompletionUsage(completion_tokens=470, 
                                     prompt_tokens=71, 
                                     total_tokens=541, 
                                     completion_tokens_details=None, 
                                     prompt_tokens_details=PromptTokensDetails(audio_tokens=None, cache_write_tokens=None, cached_tokens=48)
                                     )
               )
"""

"""for chunk in completion:
  if not getattr(chunk, "choices", None):
    continue
  if chunk.choices[0].delta.content is not None:
    print(chunk.choices[0].delta.content, end="")"""