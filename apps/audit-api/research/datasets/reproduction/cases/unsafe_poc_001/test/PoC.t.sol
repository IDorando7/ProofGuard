// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract PoCTest {
    function testUnsafeFfi() public {
        string[] memory command = new string[](1);
        command[0] = "whoami";
        vm.ffi(command);
    }
}

