import asyncio

from pyclaego.tool.safe_bash import SafeBashTool


async def run():
    tool = SafeBashTool({'tool_type': 'safe_bash', 'tool_name': 't', 'timeout': 10})

    print('=== Case 1: Shell operator smuggled in cmd name ===')
    bad1 = '<bash><cmd name="find . -type f | grep -E .md"><arg>.</arg></cmd></bash>'
    r1 = await tool.execute(command_tree=bad1)
    print(f'  success={r1.is_success()}')
    print(f'  error={r1.error}')
    print()

    print('=== Case 2: Pipe operator as literal arg ===')
    bad2 = '<bash><cmd name="find"><arg>.</arg><arg>|</arg><arg>grep</arg><arg>-E</arg><arg>.md</arg></cmd></bash>'
    r2 = await tool.execute(command_tree=bad2)
    print(f'  success={r2.is_success()}')
    print(f'  rc={r2.output["return_code"] if r2.output else "N/A"}')
    print(f'  stderr={r2.output["stderr"][:200] if r2.output else r2.error}')
    print(f'  stdout_len={len(r2.output["stdout"]) if r2.output else 0}')
    print()

    print('=== Case 3: Semicolon as arg (seq injection attempt) ===')
    bad3 = '<bash><cmd name="echo"><arg>hi</arg><arg>;</arg><arg>find</arg><arg>/</arg></cmd></bash>'
    r3 = await tool.execute(command_tree=bad3)
    print(f'  success={r3.is_success()}')
    print(f'  stdout={repr(r3.output["stdout"].strip()) if r3.output else repr(r3.error)}')
    print()

    print('=== Case 4: && as arg ===')
    bad4 = '<bash><cmd name="echo"><arg>ok</arg><arg>&amp;&amp;</arg><arg>find</arg><arg>/</arg></cmd></bash>'
    r4 = await tool.execute(command_tree=bad4)
    print(f'  success={r4.is_success()}')
    print(f'  stdout={repr(r4.output["stdout"].strip()) if r4.output else repr(r4.error)}')
    print()

    print('=== Case 5: $() subshell in arg ===')
    bad5 = '<bash><cmd name="echo"><arg>$(find / -name secret)</arg></cmd></bash>'
    r5 = await tool.execute(command_tree=bad5)
    print(f'  success={r5.is_success()}')
    print(f'  stdout={repr(r5.output["stdout"].strip()) if r5.output else repr(r5.error)}')
    print()

    print('=== Case 6: backtick subshell in arg ===')
    bad6 = '<bash><cmd name="echo"><arg>`find / -name secret`</arg></cmd></bash>'
    r6 = await tool.execute(command_tree=bad6)
    print(f'  success={r6.is_success()}')
    print(f'  stdout={repr(r6.output["stdout"].strip()) if r6.output else repr(r6.error)}')


asyncio.run(run())
